"""The single place a router turns an authenticated principal into a `Scope`.

Wave 3, Contract 4. Every router obtains its :class:`~app.backend.pg.engine.Scope`
from :func:`scope_for_principal` and from nowhere else. The routers today each
build their own `Scope` inline -- `api/budget.py::_scope_for` and
`api/audit.py::_audit_service_scope` both derive `read_all` from a set of role
NAMES, which Contract 2 forbids in as many words ("no `read_all` by role as a
substitute for a grant"). This module exists so there is exactly one
implementation to review, and so the properties below are provable once
instead of per router.

The five properties, stated so every branch below can be checked against them
-------------------------------------------------------------------------
1. **Grants are resolved, never invented.** The only source of scope is
   :func:`app.backend.pg.roles.resolve_scope`, reading `user_scope_restriction`
   and `user_scope_grant`. No role name, no header, no request field
   contributes an id or widens a dimension.

2. **Three states survive end to end.** `None` (no restriction row --
   unrestricted), `frozenset()` (restricted, zero grants -- NOTHING) and
   `frozenset({ids})` (restricted to those ids) are distinct here exactly as
   they are in `Scope`, `repo.compile_scope` and RLS. `frozenset()` is never
   normalised into `None`; that collapse turns "no grants" into "all rows" and
   is the defect class this wave exists to close.

3. **No fallback may widen.** Every failure path -- an unknown principal, an
   empty principal, a resolver that raises, a resolver that returns something
   that is not a `Scope`, a malformed dimension -- returns
   :func:`denied_scope`: every dimension `frozenset()`, `read_all` false. That
   compiles to ``FALSE`` in `repo.compile_scope` and matches no row under RLS.
   There is no code path in this module that produces an unrestricted `Scope`
   out of a failure.

4. **`read_all` comes only from `user_access_flag`,** via `resolve_scope`,
   and is only ever turned OFF here, never on. It is switched off for a
   principal that carries any restriction row, because `compile_scope`
   short-circuits to ``TRUE`` on `read_all` before it looks at a single
   dimension -- so `read_all` alongside a restriction is an unrestricted
   scope, which Contract 4 forbids returning for a principal that has
   restriction rows. Narrowing only; it can never admit a row that the flag
   alone would have denied.

5. **A SERVICE principal is an ordinary principal.** It is resolved through
   the same `resolve_scope`, gets `principal_kind='SERVICE'` recorded on its
   `Scope` (so RLS and the audit trail can see what kind of actor acted), and
   receives no additional authority whatsoever -- specifically it never gets
   :meth:`Scope.system`, whose own docstring says "for migrations and start-up
   checks only, never for a request". A service account with no grants sees
   nothing, exactly as a human with no grants does. `roles.py` separately
   guarantees a SERVICE principal can hold no maker-checker role; this module
   does not duplicate that check, it simply grants no bypass that would make
   it moot.

Two further notes on deliberate choices
---------------------------------------
**The principal must exist in `app_user`.** `resolve_scope` alone cannot tell
"this user id names nobody" from "this user is configured with no restriction
rows": both produce four `None` dimensions, i.e. *unrestricted*. A typo'd,
revoked or fabricated user id would therefore resolve to whole-estate access.
:func:`scope_for_principal` looks the id up in `app_user` first and denies when
there is no row. This is a real behaviour change for any caller whose
authenticated identity has no `app_user` row -- see the note in
`resolve_scope_with_reason` -- and it is the fail-closed direction, which
Contract 2 requires.

**A literal ``*`` grant is an ordinary id here, never a wildcard.** Contract 1:
"There is no value any id can take that means unrestricted." A grant row whose
`scope_value` is ``*`` therefore stays a member of a restricted frozenset and
matches only a row whose id is literally ``*``. This module contains no branch
that reads ``*`` as "all"; :data:`WILDCARD_LOOKALIKE` names the value only so
tests and callers can assert its absence from any widening path.
"""
from __future__ import annotations

import contextlib

import logging
from collections.abc import Iterable, Mapping
from collections.abc import Iterator
from typing import Any

from . import roles as roles_module
from .engine import (InvalidScopeValue, Scope, Session,
                     validate_scope_value)

log = logging.getLogger(__name__)

#: Keys, in order, from which the principal's id is read. Matches what
#: `api/budget.py` and `api/audit.py` already look for, so the lead's call-site
#: change is a drop-in rather than a change of identity semantics.
PRINCIPAL_ID_KEYS: tuple[str, ...] = ("user_id", "username")

#: The `Scope` fields carrying the four scope dimensions, in `Scope`'s order.
SCOPE_DIMENSION_FIELDS: tuple[str, ...] = (
    "entity_ids", "plant_ids", "project_ids", "location_ids",
)

#: The value a pre-Wave-3 grant might carry meaning "everything". It is NOT
#: treated as a wildcard anywhere in this module (Contract 1); this constant
#: exists so a test can assert that, not so a branch can special-case it.
WILDCARD_LOOKALIKE = "*"

#: The user id a denied scope carries when the principal named none at all.
#: Any value works -- a denied scope matches no row regardless -- but a stable,
#: obviously-not-a-real-id string keeps audit rows readable.
ANONYMOUS_USER_ID = "UNKNOWN"

#: Principal kinds `app_user.principal_kind` permits.
PRINCIPAL_KINDS: frozenset[str] = frozenset({"USER", "SERVICE"})


def denied_scope(user_id: str = ANONYMOUS_USER_ID,
                 principal_kind: str = "USER") -> Scope:
    """A `Scope` that can see nothing, on every dimension.

    Every dimension is `frozenset()` -- "restricted, zero grants" -- which
    `repo.compile_scope` compiles to ``FALSE`` and RLS matches no row for.
    `read_all` is false, because `read_all` short-circuits `compile_scope` to
    ``TRUE`` and would undo all four.

    This is the ONLY value any failure path in this module returns. Note what
    it is not: it is not four `None`s (that is *unrestricted*), and it is not
    an omitted dimension (`compile_scope` refuses a query that cannot express
    a restriction, which is also closed, but noisily and for a different
    reason).
    """
    return Scope(
        user_id=user_id,
        principal_kind=principal_kind if principal_kind in PRINCIPAL_KINDS else "USER",
        entity_ids=frozenset(),
        plant_ids=frozenset(),
        project_ids=frozenset(),
        location_ids=frozenset(),
        read_all=False,
    )


def is_denied(scope: Scope) -> bool:
    """True when `scope` sees nothing at all.

    Deliberately a property of the VALUE, not a flag set by this module: a
    scope built by hand with the same shape is equally denied, and a scope
    that acquired `read_all` somewhere would not pass this check even if it
    kept four empty frozensets.
    """
    if scope.read_all:
        return False
    return all(
        getattr(scope, field) == frozenset() for field in SCOPE_DIMENSION_FIELDS
    )


def principal_id(principal: Mapping[str, Any] | None) -> str | None:
    """The principal's id, or `None` when it names nobody.

    `None` is returned for a missing mapping, a mapping with none of
    :data:`PRINCIPAL_ID_KEYS`, and a value that is empty or whitespace once
    stringified. Every one of those is a resolution failure, and the caller
    fails closed on it -- an empty principal has never been an anonymous
    superuser here and must not become one.
    """
    if not isinstance(principal, Mapping):
        return None
    for key in PRINCIPAL_ID_KEYS:
        value = principal.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalise_dimension(value: Any) -> frozenset[str] | None:
    """Coerce one resolved dimension to `None` or a `frozenset[str]`.

    `None` is preserved exactly -- it is "unrestricted", a real state, not a
    missing value to be filled in. Any other iterable of scalars becomes a
    frozenset of their string forms, so a resolver returning a `set`, `list`
    or `tuple` is accepted without the three states blurring. Anything else
    (a bare string, an int, an object) raises :class:`_MalformedScope`, which
    the caller turns into a denial: a dimension we cannot read is a dimension
    we must not widen.

    A string is rejected on purpose. `frozenset("ENT-1")` is the set of its
    CHARACTERS -- an eight-element scope of single letters, which would both
    lose the real grant and silently admit rows whose id is one character
    long. That is a widening bug wearing a plausible shape.
    """
    if value is None:
        return None
    if isinstance(value, frozenset):
        candidates: Iterable[Any] = value
    elif isinstance(value, (set, list, tuple)):
        candidates = value
    else:
        raise _MalformedScope(
            f"scope dimension has type {type(value).__name__}; expected None "
            f"(unrestricted) or a frozenset/set/list/tuple of ids")
    out: set[str] = set()
    for item in candidates:
        if item is None or isinstance(item, (bytes, bytearray)):
            raise _MalformedScope(
                f"scope dimension contains a non-id member {item!r}")
        text = str(item)
        if not text:
            raise _MalformedScope("scope dimension contains an empty id")
        out.add(text)
    return frozenset(out)


class _MalformedScope(ValueError):
    """Internal: the resolver returned something this module cannot read.

    Never raised out of :func:`scope_for_principal` -- it is caught and turned
    into a denial. Callers that want the reason use
    :func:`resolve_scope_with_reason`.
    """


def resolve_scope_with_reason(
    session: Session,
    principal: Mapping[str, Any] | None,
    *,
    principal_kind: str = "USER",
) -> tuple[Scope, str | None]:
    """:func:`scope_for_principal`, plus why it denied.

    Returns ``(scope, reason)``. `reason` is `None` on success and a short
    human-readable string on every denial. Split out from
    :func:`scope_for_principal` so a test can assert WHICH failure fired
    without this module keeping mutable state, and so a router that wants to
    log the cause can, without the reason ever influencing the scope.

    Resolution order, each step failing closed:

    1. The principal must name an id (:func:`principal_id`).
    2. That id must have an `app_user` row. `app_user.principal_kind` is the
       server's own record of what kind of actor this is and OVERRIDES the
       `principal_kind` argument, which is only a caller's assertion. See the
       module docstring for why a missing row denies rather than resolving to
       four unrestricted dimensions.
    3. `roles.resolve_scope` supplies the grants. Any exception denies.
    4. The result must be a `Scope` for this user, with readable dimensions.
    5. `read_all` is cleared if any dimension is restricted (property 4).
    """
    claimed_kind = principal_kind if principal_kind in PRINCIPAL_KINDS else "USER"

    user_id = principal_id(principal)
    if user_id is None:
        return (denied_scope(ANONYMOUS_USER_ID, claimed_kind),
                "principal names no user id")

    # --- 2. the principal must be a real, known actor -------------------
    try:
        actual_kind = roles_module.resolve_principal_kind(session, user_id)
    except Exception as exc:                       # noqa: BLE001 - deny on anything
        log.warning("principal_kind lookup failed for %s: %s",
                    user_id, type(exc).__name__)
        return (denied_scope(user_id, claimed_kind),
                f"principal kind lookup raised {type(exc).__name__}")
    if actual_kind is None:
        return (denied_scope(user_id, claimed_kind),
                f"no app_user row for {user_id!r}")
    if actual_kind not in PRINCIPAL_KINDS:
        return (denied_scope(user_id, claimed_kind),
                f"app_user.principal_kind is {actual_kind!r}, not one of "
                f"{sorted(PRINCIPAL_KINDS)}")

    # --- 3. the grants themselves ---------------------------------------
    try:
        resolved = roles_module.resolve_scope(
            session, user_id, principal_kind=actual_kind)
    except Exception as exc:                       # noqa: BLE001 - deny on anything
        log.warning("scope resolution failed for %s: %s",
                    user_id, type(exc).__name__)
        return (denied_scope(user_id, actual_kind),
                f"resolve_scope raised {type(exc).__name__}")

    # --- 4. it must be a Scope we can read -------------------------------
    if not isinstance(resolved, Scope):
        return (denied_scope(user_id, actual_kind),
                f"resolve_scope returned {type(resolved).__name__}, not a Scope")

    try:
        dimensions = {
            field: _normalise_dimension(getattr(resolved, field))
            for field in SCOPE_DIMENSION_FIELDS
        }
    except _MalformedScope as exc:
        return (denied_scope(user_id, actual_kind),
                f"resolve_scope returned an unreadable scope: {exc}")

    if str(resolved.user_id) != user_id:
        # The resolver answered about a different principal. Whatever it read,
        # it is not this caller's grant set.
        return (denied_scope(user_id, actual_kind),
                f"resolve_scope answered for {resolved.user_id!r}, "
                f"not {user_id!r}")

    # --- 5. read_all: from the flag, and only ever turned off ------------
    read_all = resolved.read_all is True   # a non-bool truthy value is not `true`
    restricted = [f for f, v in dimensions.items() if v is not None]
    if read_all and restricted:
        # Contract 4: never return an unrestricted scope for a principal that
        # has restriction rows. `compile_scope` short-circuits to TRUE on
        # read_all, so leaving it set here would ignore every one of them.
        log.warning(
            "user %s carries read_all together with restriction rows on %s; "
            "read_all cleared -- the restrictions win",
            user_id, sorted(restricted))
        read_all = False

    # Validate the stored grant values HERE, at resolution.
    #
    # `Scope(...)` does not validate; `repo.compile_scope` does. So a
    # pre-Wave-3 literal '*' row -- which migration 007's CHECK constraint
    # refuses today but an older database may still carry -- would build a
    # Scope happily and then raise InvalidScopeValue from the first query,
    # surfacing as a 500 on every route that principal touches.
    #
    # DENY instead. Contract 2: a principal whose scope cannot be resolved
    # fails closed, and an unresolvable scope is a security condition, not a
    # server fault. Denying is also the only safe reading of such a row: '*'
    # meant "unrestricted" to the old RLS predicate and "an id matching
    # nothing" to compile_scope. Those are opposites, and migration 007
    # refuses to guess between them for exactly the same reason.
    try:
        for field, values in dimensions.items():
            if values is None:
                continue
            for value in values:
                validate_scope_value(value, dimension=field)
        scope = Scope(
            user_id=user_id,
            principal_kind=actual_kind,
            entity_ids=dimensions["entity_ids"],
            plant_ids=dimensions["plant_ids"],
            project_ids=dimensions["project_ids"],
            location_ids=dimensions["location_ids"],
            read_all=read_all,
        )
    except InvalidScopeValue as exc:
        # A stored grant the Scope type refuses -- in practice a pre-Wave-3
        # literal '*' row that migration 007's CHECK constraint would refuse
        # today but that an older database may still carry.
        #
        # DENY rather than raise. Contract 2: a principal whose scope cannot
        # be resolved fails closed. Letting InvalidScopeValue escape would
        # surface as a 500 from every route that principal touches, and an
        # unresolvable scope is a security condition, not a server fault.
        #
        # Denying is also the only safe reading: '*' meant "unrestricted" to
        # the old RLS predicate and "an id matching nothing" to compile_scope,
        # so the row's intent is genuinely ambiguous and the two possible
        # answers are opposites. Migration 007 refuses to guess for the same
        # reason.
        log.warning("scope for %s carries a value Scope refuses: %s",
                    user_id, exc)
        return (denied_scope(user_id, actual_kind),
                f"stored grant rejected by Scope: {exc}")

    return (scope, None)


def scope_for_principal(session: Session, principal: Mapping[str, Any] | None,
                        *, principal_kind: str = "USER") -> Scope:
    """The `Scope` for an authenticated principal. Contract 4's one function.

    `principal` is the router's principal mapping (`request.state.*_principal`);
    its id is read from :data:`PRINCIPAL_ID_KEYS`. `principal_kind` is the
    caller's ASSERTION about the actor, used only to label a denied scope --
    the authoritative kind comes from `app_user`.

    Never raises for a caller-supplied condition, and never returns an
    unrestricted scope on a failure: every failure yields :func:`denied_scope`,
    which sees nothing. Use :func:`resolve_scope_with_reason` when the caller
    wants to know why.
    """
    scope, reason = resolve_scope_with_reason(
        session, principal, principal_kind=principal_kind)
    if reason is not None:
        log.warning("scope denied: %s", reason)
    return scope


# ===========================================================================
# The router entry point
# ===========================================================================
@contextlib.contextmanager
def _resolution_session(database: Any) -> Iterator[Session]:
    """A short transaction used only to read the caller's grants.

    It runs under a DENIED scope, which is safe and deliberate: the identity
    tables `resolve_scope` reads (`app_user`, `user_access_flag`,
    `user_scope_restriction`, `user_scope_grant`) carry no RLS policy, so the
    lookup succeeds, while every business table is closed to it. If someone
    later adds a scoped read to the resolution path it will return nothing
    rather than quietly running unrestricted.
    """
    with database.session(denied_scope()) as session:
        yield session


def scope_for_request(database: Any, principal: Mapping[str, Any] | None,
                       *, principal_kind: str = "USER") -> Scope:
    """The `Scope` a router should open its real session with.

    Routers build a `Scope` BEFORE they have a session, and resolving grants
    needs one -- so this opens a short resolution transaction first and
    returns the resolved scope for the caller to use.

    That is two transactions per request rather than one. The first is three
    indexed primary-key lookups, and the alternative -- resolving inside the
    business transaction and re-applying `SET LOCAL` partway through -- means
    a window where the transaction is open under one scope and continues under
    another. Paying for a second short transaction is the cheaper mistake.

    Any failure denies, for the same reason `scope_for_principal` does: a
    scope that cannot be resolved is a security condition, not a server fault,
    and a router that cannot establish who is asking must not fall back to
    asking for everything.
    """
    user_id = principal_id(principal) or ANONYMOUS_USER_ID
    try:
        with _resolution_session(database) as session:
            return scope_for_principal(session, principal,
                                        principal_kind=principal_kind)
    except Exception as exc:                       # noqa: BLE001 - deny on anything
        log.warning("scope resolution transaction failed for %s: %s",
                    user_id, type(exc).__name__)
        return denied_scope(user_id, principal_kind)
