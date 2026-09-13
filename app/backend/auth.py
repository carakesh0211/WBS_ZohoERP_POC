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
    # --- Fable 5.1 / 034: internal material fulfilment -------------------
    # The fulfilment decision on an approved line is a procurement call; the
    # request is raised by the roles that raise purchase requests; approval
    # is maker-checker (`imr.approve` is in MAKER_CHECKER); allocation,
    # issue, return, transfer and consumption are stores operations held by
    # the procurement side; cancel by the same. Auditor reads only.
    "fulfilment.decide":      ("ProcurementApprover", "BudgetController"),
    # Auditor is deliberately ABSENT from `imr.read`: AUD-C-006 pins the
    # Auditor to an allow-list of five reads, and widening it is a product
    # decision (D-12's shape), not a side effect of a new screen. Recorded
    # in the delivery status as an open decision for the owner.
    "imr.read":               ("Requestor", "BudgetController", "ProcurementApprover",
                               "FinanceApprover", "CapitalisationApprover",
                               "Administrator"),
    "imr.create":             ("Requestor", "BudgetController"),
    "imr.approve":            ("ProcurementApprover",),
    "imr.allocate":           ("ProcurementApprover", "BudgetController"),
    "imr.issue":              ("ProcurementApprover", "BudgetController"),
    "imr.cancel":             ("ProcurementApprover", "BudgetController"),
    "revision.create":        ("Requestor", "BudgetController"),
    # Closing an accounting period freezes what can still be posted into it,
    # so it is a finance control, not an administrative convenience. Auditor
    # is deliberately absent: the role is read-only.
    "period.transition":      ("BudgetController", "FinanceApprover"),
    # --- Fable 5.1 / M-3: reopening a CLOSED period ------------------------
    # Two permissions, because the reopen is a two-step path and the two steps
    # are held by different hands. `period.reopen` is the MAKER's right: to
    # raise a reopen request against an approval instance. The roles that may
    # close a period may also ask for it to be reopened.
    "period.reopen":          ("BudgetController", "FinanceApprover"),
    # `period.reopen.apply` is the CHECKER's right: to apply or refuse an
    # outstanding request. It is in MAKER_CHECKER, so the requester can never
    # also be the applier -- `api/budget.py` compares the two through
    # `require_separation(require_maker=True)` before the engine is called,
    # and `pg/periods.py` compares the CLOSER against the applier a second
    # time, unconditionally. Auditor holds neither: a reopen is not a read.
    "period.reopen.apply":    ("FinanceApprover", "CapitalisationApprover"),
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
    # Publishing a saved view to everyone in an entity. NOT the same right as
    # saving one: a PRIVATE view is a bookmark under the caller's own identity
    # and correctly sits at the router floor, which every role holds. A SHARED
    # view is opened by other people, who then read money through whatever
    # filters it carries -- so it is a write that affects other principals.
    #
    # Held by the roles that already curate configuration others depend on.
    # Auditor is excluded for the same reason it is excluded from
    # `reconciliation.triage`: `test_aud_c_006_auditor_is_read_only` pins the
    # Auditor as read-only, and publishing into an entity is not a read.
    # Recorded against D-12 with the other role-mapping placeholders; it is a
    # documented default, not a resolved client decision.
    "report.view.share":      ("Administrator", "BudgetController",
                               "FinanceApprover"),
    "connector.read":         ("Administrator", "Auditor"),
    "connector.manage":       ("Administrator",),
    # --- Wave 7: asynchronous exports --------------------------------
    # Starting, advancing, cancelling or retrying an export job. READING a
    # job needs only the router's `budget.read` floor: a caller can only ever
    # see their own jobs, and refusing them sight of their own queued work
    # would be a permission that protects nothing.
    #
    # This grants NO data right. An export runs under the requester's own
    # resolved scope, captured on the job row, and contains no row they could
    # not already read -- what it decides is who may cause a rendered copy of
    # scoped financial data to exist outside the tables RLS protects.
    #
    # Auditor is deliberately absent, and this is the one place that costs
    # something: an auditor cannot start an export. Creating a job is a row
    # INSERT, `test_aud_c_006_auditor_is_read_only` pins Auditor to an
    # allow-list of four permissions, and widening an audit-finding assertion
    # so a new feature reads tidily is not a call to make in passing. An
    # Auditor reads through `/api/audit` and the report API. Recorded against
    # D-12 alongside the other role-mapping placeholders.
    "export.create":          ("Requestor", "BudgetController", "ProcurementApprover",
                               "FinanceApprover", "CapitalisationApprover",
                               "Administrator"),
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
    # --- Fable 5.1: original budget creation (U1 "Create budget") ------
    # Drafting, editing, submitting, cancelling and importing an ORIGINAL
    # budget document. Approval is NOT a permission here: it is the approval
    # engine's assignment plus maker-checker, decided per instance, and the
    # engine's `approval.act` floor. Auditor is read-only (budget.read).
    "budget.create":          ("Requestor", "BudgetController", "FinanceApprover",
                               "Administrator"),
    # The budget CATEGORY master (separate from budget_head, AMB-04).
    "budget.category.manage": ("Administrator", "BudgetController"),
    # --- Fable 5.1: exchange-rate administration (migration 028) ----------
    # Reading the rate book is wide: every role that reads a bill, a budget
    # or an audit trail needs to see the rate a figure was translated at.
    # Recording, activating and retiring a rate is the finance controller's
    # and the administrator's; activation is additionally maker-checker
    # separated by fx_policy (FX_SELF_ACTIVATION), not by a third permission.
    # THE AUDITOR READS THE RATE BOOK: decided by the product owner on
    # 2026-09-11 (Fable 5.1; recorded against D-12 and AUD-C-006). A read,
    # never fx.manage; tests/test_security_identity.py holds the exact
    # Auditor set with this permission named.
    "fx.read":                ("Requestor", "BudgetController", "ProcurementApprover",
                               "FinanceApprover", "CapitalisationApprover",
                               "Auditor", "Administrator"),
    "fx.manage":              ("FinanceApprover", "Administrator"),
}

# Approval permissions are subject to maker-checker: the approver may not be the
# person who raised the object.
# --- Fable 5.1 / Stream B (product owner, 2026-09-13) ---------------------
# THE ADMINISTRATOR HOLDS EVERY PERMISSION. Applied here, once, over the
# catalogue above rather than typed into each tuple, so a permission added
# later cannot omit the role by accident and a reviewer can see the rule in
# one place. Maker-checker is NOT weakened by it: an Administrator who raised
# an object is still refused its approval by `require_separation` unless they
# override DELIBERATELY -- an explicit `admin_override_reason`, the role, no
# delegation -- and every override is an ADMIN_SELF_APPROVAL_OVERRIDE audit
# entry (`pg/admin_override.py`). This supersedes the AUD-C-006 assertion
# "administration must not be a route to approving spend" by owner decision;
# tests/ADAPTATIONS.md records it.
ADMIN_ROLE = "Administrator"
ADMIN_OVERRIDE_ACTION = "ADMIN_SELF_APPROVAL_OVERRIDE"
ADMIN_OVERRIDE_MIN_REASON = 10
PERMISSIONS = {
    permission: (roles if ADMIN_ROLE in roles else (*roles, ADMIN_ROLE))
    for permission, roles in PERMISSIONS.items()
}



MAKER_CHECKER = {"pr.approve", "pr.approve_exception", "imr.approve",
                 "revision.approve",
                 "capitalisation.approve", "bill.void", "period.reopen.apply"}


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


def admin_override_permitted(principal_: dict) -> bool:
    """Whether this principal may override a self-approval at all."""
    return ADMIN_ROLE in (principal_.get("roles") or ())


def require_separation(principal_: dict, permission: str, maker_user_id: str | None,
                       *, object_label: str = "this item",
                       require_maker: bool = False,
                       admin_override_reason: str | None = None) -> dict | None:
    """Maker-checker. The person who raised something may never approve it --
    unless they are the Administrator overriding DELIBERATELY (Stream B).

    Returns None when there is no separation issue. Returns the override
    RECORD (a dict the services hand to `pg.admin_override.record`) when the
    caller is the maker, holds the Administrator role and supplied an
    `admin_override_reason` of at least ADMIN_OVERRIDE_MIN_REASON characters.
    Raises `SELF_APPROVAL` when the caller is the maker and offered no reason
    (the default, unchanged); `ADMIN_OVERRIDE_NOT_PERMITTED` when a reason
    came from any other role; `ADMIN_OVERRIDE_REASON_REQUIRED` when the reason
    is too short to explain anything.

    A falsy ``maker_user_id`` means "this caller did not tell me who the maker
    is". By default that is PERMISSIVE, and deliberately so: several callers
    resolve the maker from a row they may not be able to see yet, and turning
    "not found" into a segregation refusal would answer the wrong question
    with the wrong status code.

    That default is also how ``bill.void`` shipped a control that never ran:
    ``services.void_bill`` read a ``created_by`` column the `bill` table did
    not have, so the maker was always ``None``, the ``and`` short-circuited,
    and the function returned having compared nobody. Callers that MUST know
    the maker now say so with ``require_maker=True`` and get a refusal instead
    of silent permission -- the same reasoning as an empty scope meaning
    "nothing", never "everything".
    """
    if permission not in MAKER_CHECKER:
        return
    if not maker_user_id:
        if require_maker:
            raise AuthError(
                403, "MAKER_UNKNOWN",
                f"{object_label} does not record who raised it, so segregation "
                f"of duties cannot be verified. It cannot be approved until the "
                f"maker is known.")
        return
    if principal_["user_id"] != maker_user_id:
        return None
    if admin_override_reason is None:
        raise AuthError(
            403, "SELF_APPROVAL",
            f"You raised {object_label} and cannot also approve it. "
            f"Segregation of duties requires an independent approver."
            + (" An Administrator may override this deliberately by supplying "
               "admin_override_reason; the override is audited."
               if admin_override_permitted(principal_) else ""))
    if not admin_override_permitted(principal_):
        raise AuthError(
            403, "ADMIN_OVERRIDE_NOT_PERMITTED",
            f"You raised {object_label} and cannot also approve it. Only an "
            f"Administrator may override segregation of duties, and your role "
            f"({', '.join(principal_.get('roles') or []) or 'none'}) is not one.")
    reason = str(admin_override_reason).strip()
    if len(reason) < ADMIN_OVERRIDE_MIN_REASON:
        raise AuthError(
            422, "ADMIN_OVERRIDE_REASON_REQUIRED",
            f"An administrator override of segregation of duties on "
            f"{object_label} needs an explicit reason of at least "
            f"{ADMIN_OVERRIDE_MIN_REASON} characters; it is written into the "
            f"audit trail.")
    return {
        "action": ADMIN_OVERRIDE_ACTION,
        "actor_user_id": principal_["user_id"],
        "maker_user_id": maker_user_id,
        "permission": permission,
        "object_label": object_label,
        "reason": reason,
    }


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
    # Fable 5.1 (2026-09-11): the Zoho ERP demo organisation's four users,
    # Administrators by the product owner's decision ("everyone tests").
    ("U-RAKESH",   ["Administrator"]),
    ("U-PRITHA",   ["Administrator"]),
    ("U-SURAJ",    ["Administrator"]),
    ("U-ABHISHEK", ["Administrator"]),
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


# ---------------------------------------------------------------- UAT preview profile
# Fable 5.1. A hosted UAT preview cannot run on the seeded `<user-id>!demo`
# scheme above: the sign-in page lists the user ids, so every password is one
# guess away. Under CAPEX_PROFILE=uat-preview the boot path (app/run.py)
# refuses to call `provision_dev_identities` at all and instead loads
# credential HASHES from a file that is generated OUTSIDE the repository by
# tools/appsail/uat_credentials.py. No plaintext password ever enters this
# process: the file carries PBKDF2 salt+hash pairs only, and the loader
# refuses a file that carries anything password-shaped.
UAT_PROFILE = "uat-preview"
UAT_CREDENTIALS_ENV = "CAPEX_UAT_CREDENTIALS"
_HEX_RE = None


def is_uat_profile() -> bool:
    """True under CAPEX_PROFILE=uat-preview. Distinct from `is_demo_profile`
    on purpose: the UAT profile enables NOTHING destructive (`/api/admin/reset`
    stays refused) and DISABLES the derivable demo credentials."""
    return os.environ.get("CAPEX_PROFILE", "").lower() == UAT_PROFILE


class UatCredentialsError(RuntimeError):
    """The credential file is missing, malformed, or carries a plaintext
    password. Boot refuses; nothing is provisioned."""


def load_uat_credentials(path: str) -> dict[str, dict[str, str]]:
    """Read `{ "users": { "<user_id>": {"salt": <hex>, "hash": <hex>} } }`.

    Fails closed on: a missing file, a non-object, an unknown user id, a
    malformed salt/hash, ANY key other than salt/hash on a user entry (a
    `password` key means somebody wrote plaintext into the file), and fewer
    than one user. The returned mapping is the only thing the caller sees.
    """
    import json
    import re
    global _HEX_RE
    if _HEX_RE is None:
        _HEX_RE = re.compile(r"^[0-9a-f]+$")
    if not path or not os.path.isfile(path):
        raise UatCredentialsError(
            f"UAT credentials file not found ({UAT_CREDENTIALS_ENV}={path!r}). "
            "Generate one OUTSIDE the repository with "
            "`python tools/appsail/uat_credentials.py --generate <dir>`.")
    with open(path, "r", encoding="utf-8") as fh:
        try:
            doc = json.load(fh)
        except ValueError as exc:
            raise UatCredentialsError(f"UAT credentials file is not valid JSON ({type(exc).__name__}).")
    users = doc.get("users") if isinstance(doc, dict) else None
    if not isinstance(users, dict) or not users:
        raise UatCredentialsError("UAT credentials file must carry a non-empty 'users' object.")
    known = {uid for uid, _ in DEV_USERS}
    out: dict[str, dict[str, str]] = {}
    for user_id, entry in users.items():
        if user_id not in known:
            raise UatCredentialsError(f"UAT credentials name an unknown identity {user_id!r}.")
        if not isinstance(entry, dict) or set(entry) != {"salt", "hash"}:
            raise UatCredentialsError(
                f"UAT credential entry for {user_id} must carry exactly 'salt' and 'hash' "
                f"(found {sorted(entry) if isinstance(entry, dict) else type(entry).__name__}). "
                "A plaintext password never belongs in this file.")
        salt, digest = entry["salt"], entry["hash"]
        if not (isinstance(salt, str) and _HEX_RE.match(salt) and len(salt) == 32):
            raise UatCredentialsError(f"UAT credential salt for {user_id} is malformed.")
        if not (isinstance(digest, str) and _HEX_RE.match(digest) and len(digest) == 64):
            raise UatCredentialsError(f"UAT credential hash for {user_id} is malformed.")
        out[user_id] = {"salt": salt, "hash": digest}
    return out


def provision_uat_identities(con: sqlite3.Connection,
                             credentials: dict[str, dict[str, str]]) -> int:
    """Install the UAT credential hashes and role grants. Idempotent.

    Only identities present in `credentials` get a credential row; every
    other seeded identity is DISABLED so it cannot sign in with a stale hash
    from an earlier provisioning. Roles come from `DEV_USERS`, exactly as the
    demo provisioning grants them -- the UAT preview changes who can sign in,
    never what a role may do.
    """
    now = _now().isoformat(timespec="seconds")
    n = 0
    for user_id, roles in DEV_USERS:
        if not con.execute("SELECT 1 FROM app_user WHERE user_id=?", (user_id,)).fetchone():
            continue
        entry = credentials.get(user_id)
        if entry is None:
            con.execute("UPDATE app_credential SET disabled=1 WHERE user_id=?", (user_id,))
            continue
        con.execute("""INSERT INTO app_credential (user_id,password_salt,password_hash,disabled,created_at)
                       VALUES (?,?,?,0,?)
                       ON CONFLICT(user_id) DO UPDATE SET password_salt=excluded.password_salt,
                       password_hash=excluded.password_hash, disabled=0""",
                    (user_id, entry["salt"], entry["hash"], now))
        for r in roles:
            con.execute("INSERT OR IGNORE INTO user_role (user_id, role) VALUES (?,?)", (user_id, r))
        n += 1
    con.commit()
    return n
