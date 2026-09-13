"""Resolve a user's PG-native role and scope grants into a `Scope`.

This is the identity/access layer for the PostgreSQL port -- distinct from,
and never a replacement for, `app.backend.auth` (the frozen SQLite dev
identity provider that resolves a session and enforces `PERMISSIONS` /
`MAKER_CHECKER` for the legacy API surface). That module is not modified
here. This one exists because the PostgreSQL milestones need a role and
row-level-scope model that speaks `app.backend.pg.engine.Scope` -- the
`entity_ids` / `plant_ids` / `project_ids` / `location_ids` / `read_all`
shape `repo.compile_scope` and the RLS policies in
`migrations/pg/004_identity_scope.sql` both understand.

The 13 business roles below are frozen at
`research/30_contracts/C9_roles.json` ("Every screen's 'Permission
requirements' attribute must name roles from this list only."). They are a
DIFFERENT vocabulary from `auth.ROLES` (the SQLite prototype's 7-role dev
set) -- the two are not meant to line up 1:1, and `PERMISSIONS` /
`MAKER_CHECKER` below are this module's own, keyed to the 13-role set, not a
duplicate of `auth.py`'s.

The security property this module exists to protect, stated once so every
function below can be checked against it:

    **`None` on a `Scope` dimension means unrestricted. An empty
    `frozenset` means NOTHING. A resolver that cannot tell "this user has
    no scope row for this dimension" apart from "this user was explicitly
    granted zero ids for this dimension" collapses the second case into the
    first -- turning "no grants" into "all rows", which is exactly the
    inversion `repo.compile_scope` exists to prevent. See
    `user_scope_restriction` in the migration: a dimension is restricted
    (possibly to nothing) only if it has a row there; its absence is what
    "unrestricted" means.**
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# `InvalidScopeValue` is imported (not just referenced through engine) so a
# caller can `except roles.InvalidScopeValue` around the write path that
# raises it, without reaching into engine for the type.
from .engine import InvalidScopeValue, Scope, Session, validate_scope_value

# ---------------------------------------------------------------- roles
#: Frozen at research/30_contracts/C9_roles.json, 2026-08-06. Order is the
#: source file's order; `tests/test_pg_roles.py` asserts this tuple matches
#: the JSON exactly, and `migrations/pg/004_identity_scope.sql`'s
#: `role_grant.role` CHECK constraint is hand-kept in sync with it.
ROLES: tuple[str, ...] = (
    "Requestor",
    "Project Manager",
    "Plant Head",
    "Department Head",
    "Procurement",
    "Finance",
    "Project Finance Controller",
    "CAPEX Committee",
    "CFO",
    "Management Approver",
    "System Administrator",
    "Internal Auditor",
    "Read-only Management User",
)
_ROLE_SET = frozenset(ROLES)

#: The four scope dimensions `Scope` and `repo.compile_scope` understand.
DIMENSIONS: tuple[str, ...] = ("entity", "plant", "project", "location")
_DIM_TO_SCOPE_FIELD: dict[str, str] = {
    "entity": "entity_ids",
    "plant": "plant_ids",
    "project": "project_ids",
    "location": "location_ids",
}

# ---------------------------------------------------------------- permissions
#: Permission -> the roles that hold it. This is THIS module's own catalog,
#: distinct from `auth.PERMISSIONS` (see module docstring). Names are chosen
#: to read naturally alongside the legacy vocabulary but are not required to
#: match it permission-for-permission.
PERMISSIONS: dict[str, tuple[str, ...]] = {
    "budget.read": ROLES,
    "budget.check": (
        "Requestor", "Project Manager", "Plant Head", "Department Head",
        "Finance", "Project Finance Controller", "Internal Auditor",
        "System Administrator",
    ),
    "pr.create": ("Requestor", "Project Manager"),
    "pr.approve": ("Plant Head", "Department Head"),
    "pr.approve_exception": ("Management Approver",),
    "po.amend": ("Procurement",),
    "po.cancel": ("Procurement",),
    "po.close": ("Procurement",),
    # Fable 5.1 / 034: internal material fulfilment, in this catalogue's
    # vocabulary. Mirrors `auth.PERMISSIONS`' entries role-for-role.
    "fulfilment.decide": ("Procurement", "Project Manager"),
    "imr.read": tuple(r for r in ROLES if r != "Internal Auditor"),
    "imr.create": ("Requestor", "Project Manager"),
    "imr.approve": ("Plant Head", "Department Head"),
    "imr.allocate": ("Procurement", "Project Manager"),
    "imr.issue": ("Procurement", "Project Manager"),
    "imr.cancel": ("Procurement", "Project Manager"),
    "revision.create": ("Project Manager", "Finance"),
    "revision.approve": ("Project Finance Controller", "CFO"),
    "capitalisation.allocate": ("Project Finance Controller", "Finance"),
    "capitalisation.approve": ("CAPEX Committee", "CFO"),
    "bill.void": ("Finance", "CFO"),
    "audit.read": ("Internal Auditor", "System Administrator"),
    "access.read": ("System Administrator", "Internal Auditor"),
    "access.grant": ("System Administrator",),
}

#: Fable 5.1 / Stream B (product owner, 2026-09-13): the System Administrator
#: holds EVERY permission in this catalogue, applied once over the table above
#: for the reason `auth.py` gives at the same spot. Maker-checker below is
#: unchanged; the deliberate, audited override lives in `auth.require_separation`
#: and `pg/admin_override.py`.
ADMIN_ROLE = "System Administrator"
PERMISSIONS = {
    permission: (roles if ADMIN_ROLE in roles else (*roles, ADMIN_ROLE))
    for permission, roles in PERMISSIONS.items()
}

#: Fable 5.1 / Stream B (product owner, 2026-09-13): the System Administrator
#: holds EVERY permission in this catalogue, applied once over the table above
#: for the reason `auth.py` gives at the same spot. Maker-checker below is
#: unchanged; the deliberate, audited override lives in `auth.require_separation`
#: and `pg/admin_override.py`.
ADMIN_ROLE = "System Administrator"
PERMISSIONS = {
    permission: (roles if ADMIN_ROLE in roles else (*roles, ADMIN_ROLE))
    for permission, roles in PERMISSIONS.items()
}

#: Fable 5.1 / Stream B (product owner, 2026-09-13): the System Administrator
#: holds EVERY permission in this catalogue, applied once over the table above
#: for the reason `auth.py` gives at the same spot. Maker-checker below is
#: unchanged; the deliberate, audited override lives in `auth.require_separation`
#: and `pg/admin_override.py`.
ADMIN_ROLE = "System Administrator"
PERMISSIONS = {
    permission: (roles if ADMIN_ROLE in roles else (*roles, ADMIN_ROLE))
    for permission, roles in PERMISSIONS.items()
}

#: Permissions subject to maker-checker: the same principal who raised an
#: object may never also decide it. Mirrors `auth.MAKER_CHECKER`'s role in
#: the legacy system, for this module's own permission catalog.
MAKER_CHECKER: frozenset[str] = frozenset({
    "pr.approve", "pr.approve_exception", "imr.approve", "revision.approve",
    "capitalisation.approve", "bill.void",
})


def roles_with_permission(permission: str) -> tuple[str, ...]:
    roles = PERMISSIONS.get(permission)
    if roles is None:
        raise UnknownPermission(f"unknown permission {permission!r}")
    return roles


def roles_with_maker_checker() -> frozenset[str]:
    """Every role that holds at least one maker-checker permission.

    A SERVICE principal may never hold any role in this set -- see
    `assert_role_grantable` and `assert_no_service_maker_checker`.
    """
    out: set[str] = set()
    for permission in MAKER_CHECKER:
        out.update(roles_with_permission(permission))
    return frozenset(out)


def permissions_for_roles(roles: Iterable[str]) -> frozenset[str]:
    role_set = set(roles)
    return frozenset(
        permission for permission, holders in PERMISSIONS.items()
        if role_set & set(holders)
    )


# ---------------------------------------------------------------- errors
class UnknownRole(ValueError):
    """A role outside the frozen 13 in `ROLES` was requested."""


class UnknownPermission(ValueError):
    """A permission not present in `PERMISSIONS` was requested."""


class UnknownDimension(ValueError):
    """A scope dimension outside `DIMENSIONS` was requested."""


class ServiceMakerCheckerDenied(ValueError):
    """A SERVICE principal was about to be granted a maker-checker role.

    No service account may hold a permission subject to maker-checker --
    there is no independent human on the other side of an automated actor's
    own approval. This is enforced at the write path (`set_roles`), not only
    asserted in a test, so it cannot be reintroduced by a future caller of
    this module.
    """


# ---------------------------------------------------------------- resolution
@dataclass(frozen=True)
class Grant:
    """A user's full PG-native access grant: roles, scope, and the
    permissions those roles imply. Returned by :func:`resolve_grant` and
    what `api/admin_access.py`'s GET endpoint renders."""

    user_id: str
    roles: tuple[str, ...]
    scope: Scope
    permissions: frozenset[str]


def resolve_roles(session: Session, user_id: str) -> tuple[str, ...]:
    rows = session.fetchall(
        "SELECT role FROM role_grant WHERE user_id = %s ORDER BY role", (user_id,))
    return tuple(row[0] for row in rows)


def resolve_scope(session: Session, user_id: str, *,
                   principal_kind: str = "USER") -> Scope:
    """A user's grants, resolved into the `Scope` `Database.session()` and
    `repo.compile_scope` both understand.

    For each dimension: a row in `user_scope_restriction` for `(user_id,
    dimension)` means the dimension IS restricted, to whatever
    `user_scope_grant` rows exist for that pair -- possibly none, which
    compiles to `frozenset()`, "nothing". No row in `user_scope_restriction`
    means the dimension is unrestricted -- `None`. These are read as two
    different tables specifically so "zero grant rows" (restricted-to-
    nothing) can never be mistaken for "no restriction row at all"
    (unrestricted); collapsing them was the exact bug class `repo.py`'s own
    module docstring names.
    """
    flag_row = session.fetchone(
        "SELECT read_all FROM user_access_flag WHERE user_id = %s", (user_id,))
    read_all = bool(flag_row[0]) if flag_row is not None else False

    dims: dict[str, frozenset[str] | None] = {}
    for dimension in DIMENSIONS:
        restricted = session.fetchone(
            "SELECT 1 FROM user_scope_restriction WHERE user_id = %s AND dimension = %s",
            (user_id, dimension))
        if restricted is None:
            dims[dimension] = None
            continue
        rows = session.fetchall(
            "SELECT scope_value FROM user_scope_grant "
            "WHERE user_id = %s AND dimension = %s ORDER BY scope_value",
            (user_id, dimension))
        dims[dimension] = frozenset(row[0] for row in rows)

    return Scope(
        user_id=user_id,
        principal_kind=principal_kind,
        entity_ids=dims["entity"],
        plant_ids=dims["plant"],
        project_ids=dims["project"],
        location_ids=dims["location"],
        read_all=read_all,
    )


def resolve_principal_kind(session: Session, user_id: str) -> str | None:
    row = session.fetchone(
        "SELECT principal_kind FROM app_user WHERE user_id = %s", (user_id,))
    return row[0] if row is not None else None


def resolve_grant(session: Session, user_id: str) -> Grant | None:
    """`None` if `user_id` names no `app_user` row -- the caller decides
    whether that is a 404 (an admin endpoint may say so; this is not the
    row-level-scoped business data `repo.query()` guards, so there is no
    existence-oracle concern in confirming a managed user id is unknown)."""
    principal_kind = resolve_principal_kind(session, user_id)
    if principal_kind is None:
        return None
    roles = resolve_roles(session, user_id)
    scope = resolve_scope(session, user_id, principal_kind=principal_kind)
    return Grant(user_id=user_id, roles=roles, scope=scope,
                 permissions=permissions_for_roles(roles))


# ---------------------------------------------------------------- writes
def assert_role_grantable(principal_kind: str, role: str) -> None:
    """Raise if granting `role` to a principal of `principal_kind` would
    hand a SERVICE account a maker-checker permission."""
    if role not in _ROLE_SET:
        raise UnknownRole(f"{role!r} is not one of the 13 frozen roles: {ROLES}")
    if principal_kind == "SERVICE" and role in roles_with_maker_checker():
        held = sorted(p for p in MAKER_CHECKER if role in PERMISSIONS.get(p, ()))
        raise ServiceMakerCheckerDenied(
            f"role {role!r} carries maker-checker permission(s) {sorted(held)}; "
            f"a SERVICE principal may never hold a maker-checker permission -- "
            f"there is no independent human on the other side of an automated "
            f"actor's own approval.")


def set_roles(session: Session, user_id: str, roles: Iterable[str], *,
              granted_by: str) -> tuple[str, ...]:
    """Replace `user_id`'s role grants with exactly `roles`.

    Refuses (raises, writes nothing) if the target does not exist, if any
    role is not one of the 13 frozen roles, or if the target is a SERVICE
    principal and any requested role carries a maker-checker permission.
    """
    principal_kind = resolve_principal_kind(session, user_id)
    if principal_kind is None:
        raise ValueError(f"no app_user row for {user_id!r}")

    new_roles = tuple(dict.fromkeys(roles))  # de-duplicate, preserve order
    for role in new_roles:
        assert_role_grantable(principal_kind, role)

    session.execute("DELETE FROM role_grant WHERE user_id = %s", (user_id,))
    for role in new_roles:
        session.execute(
            "INSERT INTO role_grant (user_id, role, granted_by) VALUES (%s, %s, %s)",
            (user_id, role, granted_by))
    return new_roles


def set_scope(session: Session, user_id: str,
              scopes: dict[str, list[str] | None], *, read_all: bool,
              updated_by: str) -> Scope:
    """Replace `user_id`'s scope grants.

    `scopes` maps each of `entity_ids` / `plant_ids` / `project_ids` /
    `location_ids` (a dimension missing from the mapping is treated the same
    as an explicit `None`) to either `None` -- unrestricted -- or a `list`
    of permitted ids, which may be empty -- restricted to nothing. This is
    exactly the `null` means unrestricted / `[]` means nothing contract
    `docs/WAVE2_CONTRACTS.md` states for `PUT /api/admin/users/{user_id}/grants`.

    Every id is validated by `engine.validate_scope_value` BEFORE the first
    write, so a rejected request writes nothing at all rather than leaving
    the user's grants half-replaced. This is the first of the three layers
    refusing the `*` sentinel (the others being `repo.compile_scope` and the
    `user_scope_grant_value_not_sentinel` CHECK constraint in
    `migrations/pg/007_scope_sentinel.sql`). It RAISES `InvalidScopeValue`;
    it never drops the offending value and carries on, because silently
    granting less than the caller asked for is its own defect.
    """
    principal_kind = resolve_principal_kind(session, user_id)
    if principal_kind is None:
        raise ValueError(f"no app_user row for {user_id!r}")

    # Validate the WHOLE request up front. Doing this inside the write loop
    # below would leave earlier dimensions already deleted and re-inserted
    # when a later one is refused -- correct only if every caller wraps this
    # in a transaction it remembers to roll back. Validating first makes the
    # all-or-nothing property intrinsic instead of borrowed.
    for dimension, field in _DIM_TO_SCOPE_FIELD.items():
        values = scopes.get(field, None)
        if values is None:
            continue
        for value in values:
            validate_scope_value(value, dimension=dimension)

    session.execute(
        """
        INSERT INTO user_access_flag (user_id, read_all, updated_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE
            SET read_all = EXCLUDED.read_all,
                updated_at = now(),
                updated_by = EXCLUDED.updated_by,
                version_no = user_access_flag.version_no + 1
        """,
        (user_id, read_all, updated_by))

    for dimension, field in _DIM_TO_SCOPE_FIELD.items():
        # Both `{}` (key absent) and an explicit `None` value mean
        # unrestricted -- see the docstring.
        values = scopes.get(field, None)
        session.execute(
            "DELETE FROM user_scope_restriction WHERE user_id = %s AND dimension = %s",
            (user_id, dimension))
        # The DELETE above cascades into user_scope_grant (ON DELETE CASCADE
        # via the composite FK), so there is nothing stale left to clean up
        # explicitly before re-inserting below.
        if values is None:
            continue  # unrestricted: no restriction row at all
        session.execute(
            "INSERT INTO user_scope_restriction (user_id, dimension, updated_by) "
            "VALUES (%s, %s, %s)",
            (user_id, dimension, updated_by))
        for value in dict.fromkeys(values):  # de-duplicate, preserve order
            session.execute(
                "INSERT INTO user_scope_grant (user_id, dimension, scope_value, granted_by) "
                "VALUES (%s, %s, %s, %s)",
                (user_id, dimension, value, updated_by))

    return resolve_scope(session, user_id, principal_kind=principal_kind)


def assert_no_service_maker_checker(session: Session) -> None:
    """Whole-database invariant check: no SERVICE principal holds any role
    carrying a maker-checker permission.

    `set_roles` refuses to CREATE this state; this is the independent,
    read-only proof that it does not exist regardless of how a row got
    there (a hand-run `INSERT`, a future write path that forgets to call
    `set_roles`, restored seed data, and so on). Raises `AssertionError`
    naming every offending `(user_id, role)` pair, not just the first.
    """
    maker_checker_roles = sorted(roles_with_maker_checker())
    if not maker_checker_roles:
        return
    rows = session.fetchall(
        """
        SELECT rg.user_id, rg.role
        FROM role_grant rg
        JOIN app_user u ON u.user_id = rg.user_id
        WHERE u.principal_kind = 'SERVICE' AND rg.role = ANY(%s)
        ORDER BY rg.user_id, rg.role
        """,
        (maker_checker_roles,))
    if rows:
        offenders = ", ".join(f"{user_id}:{role}" for user_id, role in rows)
        raise AssertionError(
            f"SERVICE principal(s) hold a maker-checker role: {offenders}. "
            f"No service account may hold a maker-checker permission.")
