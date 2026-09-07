"""Identity, roles and segregation of duties.

AUD-C-006. The audited build accepted an arbitrary bearer token, then (after a
concurrent change) a single shared Basic credential under which any caller could
name themselves as any actor and approve their own requests. Actor identity now
comes from a server-side session; the request body can no longer nominate who is
acting.

This is a DEVELOPMENT identity provider behind a stable interface. It is not an
enterprise IdP: there is no federation, MFA, password policy, lockout or
lifecycle management. Those remain production work (see
CORRECTION_IMPLEMENTATION_REPORT.md). What it does provide is the property the
financial controls depend on - a trustworthy, server-derived acting user with
roles that the caller cannot choose.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

SESSION_HOURS = 8
PBKDF2_ROUNDS = 240_000

# ---------------------------------------------------------------- roles
ROLES = ("Requestor", "BudgetController", "ProcurementApprover", "FinanceApprover",
         "CapitalisationApprover", "Auditor", "Administrator")

# Permission -> roles that hold it. Least privilege: a role gets a permission
# only where it is required to do the job.
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "budget.read":            ROLES,
    "budget.check":           ("Requestor", "BudgetController", "ProcurementApprover",
                               "FinanceApprover", "Auditor", "Administrator"),
    "pr.create":              ("Requestor", "BudgetController"),
    "pr.approve":             ("ProcurementApprover",),
    "pr.approve_exception":   ("FinanceApprover",),
    "po.amend":               ("ProcurementApprover",),
    "po.cancel":              ("ProcurementApprover",),
    "po.close":               ("ProcurementApprover",),
    "revision.create":        ("Requestor", "BudgetController"),
    # Closing an accounting period freezes what can still be posted into it,
    # so it is a finance control, not an administrative convenience. Auditor
    # is deliberately absent: the role is read-only.
    "period.transition":      ("BudgetController", "FinanceApprover"),
    # --- M4b, the approval engine ------------------------------------
    # `approval.read` is the router floor: an approver must be able to see
    # their own inbox. Contract 4 says "every role"; Auditor is deliberately
    # EXCLUDED, because `test_aud_c_006_auditor_is_read_only` pins Auditor to
    # an allow-list of four permissions, and widening an audit-finding
    # assertion to grant access is not a call to make silently. An Auditor
    # reads approval history through the audit chain, which is the record
    # that matters for their purpose. Recorded against D-12.
    "approval.read":          ("Requestor", "BudgetController", "ProcurementApprover",
                               "FinanceApprover", "CapitalisationApprover",
                               "Administrator"),
    # The floor for acting. WHETHER a given caller may act on a given
    # instance is not a permission question at all -- it is assignment plus
    # maker-checker, decided per decision inside the transaction.
    "approval.act":           ("Requestor", "BudgetController", "ProcurementApprover",
                               "FinanceApprover", "CapitalisationApprover"),
    "approval.configure":     ("Administrator",),
    "approval.delegate":      ("BudgetController", "ProcurementApprover",
                               "FinanceApprover", "CapitalisationApprover"),
    "revision.approve":       ("FinanceApprover",),
    "capitalisation.allocate": ("BudgetController", "FinanceApprover"),
    "capitalisation.approve": ("CapitalisationApprover",),
    "bill.void":              ("FinanceApprover",),
    "audit.read":             ("Auditor", "Administrator"),
    # Settings and master data. See D-12: the role-to-permission mapping is
    # a client sign-off, and these are least-privilege placeholders until it
    # lands, not the final matrix.
    "settings.read":          ("Requestor", "BudgetController", "ProcurementApprover",
                               "FinanceApprover", "CapitalisationApprover",
                               "Administrator"),
    "settings.write":         ("Administrator",),
    "masters.read":           ("Requestor", "BudgetController", "ProcurementApprover",
                               "FinanceApprover", "CapitalisationApprover",
                               "Administrator"),
    "masters.write":          ("Administrator", "BudgetController"),
    # Regulated tax identity. A full reveal is a distinct permission and
    # writes an audit entry; masking is never bypassed by holding read alone.
    "settings.tax_identity.reveal": ("Administrator", "FinanceApprover"),
    "masters.tax_identity.reveal":  ("Administrator", "FinanceApprover",
                                     "ProcurementApprover"),
    "connector.read":         ("Administrator", "Auditor"),
    "connector.manage":       ("Administrator",),
    # Seeing, and attributing, a reconciliation exception that could not be
    # tied to an entity. Separate from connector.manage because it is a
    # DATA-triage right over other entities' unattributed discrepancies,
    # not a connector-administration right.
    #
    # ADMINISTRATOR ONLY. Auditor was the obvious second holder and is
    # deliberately excluded: `test_aud_c_006_auditor_is_read_only` pins
    # Auditor to an allow-list, and granting a fifth permission means
    # widening an audit-finding assertion -- which is not a thing to do in
    # passing so a new feature reads more tidily. `approval.read` excludes
    # Auditor for exactly this reason (recorded against D-12), and the same
    # reasoning applies: an Auditor reads what happened through the
    # hash-chained audit trail, which is the record that matters for that
    # role. Triage is an operational act, not an audit read.
    "reconciliation.triage": ("Administrator",),
    "admin.reset":            ("Administrator",),
}

# Approval permissions are subject to maker-checker: the approver may not be the
# person who raised the object.
MAKER_CHECKER = {"pr.approve", "pr.approve_exception", "revision.approve",
                 "capitalisation.approve", "bill.void"}


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------------------------------------------------------- passwords
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return salt, dk.hex()


def verify_password(password: str, salt: str, expected: str) -> bool:
    _, got = hash_password(password, salt)
    return hmac.compare_digest(got, expected)


# ---------------------------------------------------------------- sessions
def _now():
    return datetime.now(timezone.utc)


def login(con: sqlite3.Connection, user_id: str, password: str) -> dict:
    row = con.execute("""SELECT c.*, u.name FROM app_credential c
                         JOIN app_user u ON u.user_id = c.user_id
                         WHERE c.user_id = ?""", (user_id,)).fetchone()
    if not row or row["disabled"]:
        # Same response either way: do not disclose which accounts exist.
        raise AuthError(401, "INVALID_CREDENTIALS", "Invalid user or password.")
    if not verify_password(password, row["password_salt"], row["password_hash"]):
        raise AuthError(401, "INVALID_CREDENTIALS", "Invalid user or password.")

    sid = secrets.token_urlsafe(32)
    now = _now()
    con.execute("INSERT INTO app_session (session_id,user_id,created_at,expires_at) VALUES (?,?,?,?)",
                (sid, user_id, now.isoformat(timespec="seconds"),
                 (now + timedelta(hours=SESSION_HOURS)).isoformat(timespec="seconds")))
    con.commit()
    return {"session_id": sid, "user": principal(con, user_id)}


def logout(con: sqlite3.Connection, session_id: str) -> None:
    con.execute("UPDATE app_session SET revoked_at=? WHERE session_id=?",
                (_now().isoformat(timespec="seconds"), session_id))
    con.commit()


def principal(con: sqlite3.Connection, user_id: str) -> dict:
    u = con.execute("SELECT user_id, name, role FROM app_user WHERE user_id=?",
                    (user_id,)).fetchone()
    roles = [r[0] for r in con.execute("SELECT role FROM user_role WHERE user_id=?", (user_id,))]
    return {"user_id": u["user_id"], "name": u["name"], "title": u["role"], "roles": roles}


def resolve_session(con: sqlite3.Connection, session_id: str | None) -> dict:
    """Server-derived identity. Returns the principal or raises."""
    if not session_id:
        raise AuthError(401, "NOT_AUTHENTICATED", "Sign in to continue.")
    row = con.execute("SELECT * FROM app_session WHERE session_id=?", (session_id,)).fetchone()
    if not row or row["revoked_at"]:
        raise AuthError(401, "SESSION_INVALID", "Your session is no longer valid. Sign in again.")
    if row["expires_at"] < _now().isoformat(timespec="seconds"):
        raise AuthError(401, "SESSION_EXPIRED", "Your session has expired. Sign in again.")
    return principal(con, row["user_id"])


# ---------------------------------------------------------------- authorisation
def require(principal_: dict, permission: str) -> None:
    allowed = PERMISSIONS.get(permission)
    if allowed is None:
        raise AuthError(500, "UNKNOWN_PERMISSION", f"Unknown permission {permission}.")
    if not set(principal_["roles"]) & set(allowed):
        raise AuthError(
            403, "FORBIDDEN",
            f"Your role ({', '.join(principal_['roles']) or 'none'}) cannot perform "
            f"'{permission}'. Required: {' or '.join(allowed)}.")


def require_separation(principal_: dict, permission: str, maker_user_id: str | None,
                       *, object_label: str = "this item") -> None:
    """Maker-checker. The person who raised something may never approve it."""
    if permission in MAKER_CHECKER and maker_user_id and \
            principal_["user_id"] == maker_user_id:
        raise AuthError(
            403, "SELF_APPROVAL",
            f"You raised {object_label} and cannot also approve it. "
            f"Segregation of duties requires an independent approver.")


# ---------------------------------------------------------------- provisioning
DEV_USERS = [
    # user_id, roles                                              (password = user_id + '!demo')
    ("U-REQ",  ["Requestor"]),
    ("U-PM",   ["Requestor", "BudgetController"]),
    ("U-PLH",  ["ProcurementApprover"]),
    ("U-PROC", ["ProcurementApprover"]),
    ("U-FIN",  ["FinanceApprover"]),
    ("U-PFC",  ["BudgetController", "FinanceApprover"]),
    ("U-CFO",  ["FinanceApprover", "CapitalisationApprover"]),
    ("U-AUD",  ["Auditor"]),
    ("U-ADM",  ["Administrator"]),
]


def provision_dev_identities(con: sqlite3.Connection, *, password_suffix: str = "!demo") -> int:
    """Seed development credentials and role grants. Idempotent."""
    now = _now().isoformat(timespec="seconds")
    n = 0
    for user_id, roles in DEV_USERS:
        if not con.execute("SELECT 1 FROM app_user WHERE user_id=?", (user_id,)).fetchone():
            continue
        salt, h = hash_password(user_id + password_suffix)
        con.execute("""INSERT INTO app_credential (user_id,password_salt,password_hash,disabled,created_at)
                       VALUES (?,?,?,0,?)
                       ON CONFLICT(user_id) DO UPDATE SET password_salt=excluded.password_salt,
                       password_hash=excluded.password_hash""", (user_id, salt, h, now))
        for r in roles:
            con.execute("INSERT OR IGNORE INTO user_role (user_id, role) VALUES (?,?)", (user_id, r))
        n += 1
    con.commit()
    return n


def is_demo_profile() -> bool:
    """Destructive administration is only available in an explicit local profile."""
    return os.environ.get("CAPEX_PROFILE", "").lower() == "local-demo"
