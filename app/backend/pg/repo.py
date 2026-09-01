"""The scoped query chokepoint.

Every read or write that touches row-level-scoped data goes through
:func:`query`. It exists to make row-level scope a property of the query
mechanism, not a discipline every call site has to remember:

1. **The SQL must contain a literal ``{scope}`` token.** :func:`query` raises
   :class:`ScopeTokenMissing` before ever touching the connection if it does
   not -- a caller cannot ship a scoped-looking query that forgot the
   predicate.
2. **The predicate is compiled from the caller's :class:`~app.backend.pg.engine.Scope`**,
   over whichever of the entity / plant / project / location dimensions the
   caller's SQL exposes columns for (declared via ``columns=``).
3. **``None`` means unrestricted; an empty ``frozenset`` means nothing.** This
   mirrors :class:`~app.backend.pg.engine.Scope` exactly, and the distinction
   is a security property: a bug that turns "no grants" into "all rows" is
   exactly the failure this module exists to prevent. An empty-set dimension
   compiles to a predicate that can never match (``FALSE``), never to "skip
   this filter".

An out-of-scope read must come back as **no rows**, never as an error. Raising
403 on an id lookup is an existence oracle -- it tells an unauthorised caller
that the row exists even though they cannot see it. Every caller of
:func:`query` should therefore treat zero rows as "not found", not as "denied".

This module never opens a connection or a transaction itself: it operates on
the :class:`~app.backend.pg.engine.Session` handed to it by
``Database.session()``, which is where scope is *also* applied at the
connection level via ``SET LOCAL`` (see ``engine.py``). The predicate compiled
here is a second, independent enforcement of the same scope -- defence in
depth, not a replacement for row-level security policies that may exist in the
database itself.
"""
from __future__ import annotations

from typing import Any, Mapping

# `InvalidScopeValue` is re-exported so a caller can write
# `except repo.InvalidScopeValue` around `query()`/`compile_scope()` -- the
# functions that raise it -- without importing engine for the type alone.
from .engine import InvalidScopeValue, Scope, Session, validate_scope_value  # noqa: F401

#: The token a caller's SQL must contain, literally, for `query()` to accept it.
SCOPE_TOKEN = "{scope}"

#: The dimensions `compile_scope` understands, and the `Scope` field each reads.
_DIMENSION_FIELDS: dict[str, str] = {
    "entity": "entity_ids",
    "plant": "plant_ids",
    "project": "project_ids",
    "location": "location_ids",
}


class ScopeTokenMissing(ValueError):
    """Raised when SQL handed to `query()` has no literal `{scope}` token.

    This is a programming error, not a runtime condition: it means a call site
    tried to bypass row-level scope, deliberately or by omission. It is raised
    before any statement reaches the database.
    """


class ScopeNotExpressible(RuntimeError):
    """A query cannot express a restriction the caller's scope imposes.

    Raised rather than returning a wider predicate. A query that cannot honour
    a scope must fail loudly; quietly returning more rows is the failure this
    whole module exists to prevent.
    """


def compile_scope(scope: Scope, columns: Mapping[str, str] | None = None
                   ) -> tuple[str, dict[str, Any]]:
    """Compile a `Scope` into a SQL boolean expression plus its named params.

    `columns` maps a dimension name (one of ``entity``, ``plant``, ``project``,
    ``location``) to the SQL column expression that dimension corresponds to in
    the caller's query, e.g. ``{"project": "w.project_id"}``. A dimension the
    caller's query has no column for simply is not filtered -- that is a
    statement about what this query shape can express, never an unrestricted
    grant, and it is the caller's responsibility to include every dimension
    the target table actually carries.

    Returns ``(predicate_sql, params)`` where `predicate_sql` is a boolean
    expression (already parenthesised per clause, joined with ``AND``; never
    empty -- it is at least ``"TRUE"``) and `params` is a dict of named
    parameters (``%(name)s`` style) to merge into the query's own parameters.
    Named parameters are used deliberately: `query()` must be safe regardless
    of where the ``{scope}`` token sits relative to the caller's own
    placeholders, and positional `%s` params would make that order-dependent.
    """
    # ---- the repository boundary's own refusal of the `*` sentinel --------
    # Second of the three layers (see `engine.validate_scope_value`). A
    # `Scope` can reach here without ever passing through `roles.set_scope`:
    # resolved from a database restored from before Wave 3, or constructed
    # directly by a caller. Refusing here means a `*` cannot be compiled into
    # a predicate by ANY route, so it cannot be revived as a wildcard by a
    # future reader of this module.
    #
    # Checked BEFORE the `read_all` short-circuit on purpose: a scope
    # carrying a corrupt id is a corrupt scope whether or not it also
    # happens to be unrestricted, and returning "TRUE" without looking would
    # let the bad row travel on unnoticed to somewhere that does look.
    for dimension, field in _DIMENSION_FIELDS.items():
        values = getattr(scope, field)
        if values is None:
            continue
        for value in sorted(values):
            validate_scope_value(value, dimension=dimension)

    if scope.read_all:
        # Scope.system() and any principal explicitly granted unrestricted
        # read. Documented on Scope as "for migrations and start-up checks
        # only, never for a request" -- callers that hand `query()` such a
        # scope are asserting that responsibility, not this module.
        return "TRUE", {}

    columns = {} if columns is None else dict(columns)

    # A restricted dimension the caller did not map is REFUSED, not skipped.
    #
    # This loop previously iterated over `columns`, so any dimension the caller
    # omitted contributed no clause and nobody noticed. With `columns=None` --
    # the default on both query() and query_one() -- the predicate was an
    # unconditional TRUE no matter how restrictive the scope was. A user
    # scoped to one entity, run against a query whose mapping omits `entity`,
    # read every entity's rows, and the `{scope}` token guard passed happily
    # because a token was present.
    #
    # The module's stated invariant is that an empty frozenset compiles to
    # FALSE and never to "skip this filter". That has to hold for dimensions
    # the caller forgot as well, or it is not an invariant. To waive a
    # dimension deliberately, map it to None -- visible in the call site and in
    # review, unlike an omission.
    restricted = {
        dimension for dimension, field in _DIMENSION_FIELDS.items()
        if getattr(scope, field) is not None
    }
    unexpressed = sorted(restricted - set(columns))
    if unexpressed:
        raise ScopeNotExpressible(
            f"scope restricts {unexpressed} but the query maps no column for "
            f"{'it' if len(unexpressed) == 1 else 'them'}. Add the column to "
            f"`columns`, or map the dimension to None to waive it explicitly. "
            f"Silently dropping a restriction would widen the caller's access."
        )

    clauses: list[str] = []
    params: dict[str, Any] = {}
    for index, (dimension, column) in enumerate(sorted(columns.items())):
        field = _DIMENSION_FIELDS.get(dimension)
        if field is None:
            raise ValueError(
                f"unknown scope dimension {dimension!r}; expected one of "
                f"{sorted(_DIMENSION_FIELDS)}")
        if column is None:
            # Explicitly waived by the caller. Deliberate and reviewable.
            continue
        values: frozenset[str] | None = getattr(scope, field)
        if values is None:
            # Unrestricted on this dimension: no clause contributed.
            continue
        if len(values) == 0:
            # Empty frozenset: this dimension permits nothing. The whole
            # predicate must therefore reject every row -- do not let other
            # clauses' ANY(...) accidentally re-admit rows via OR.
            return "FALSE", {}
        param_name = f"__scope_{dimension}_{index}"
        clauses.append(f"({column} = ANY(%({param_name})s))")
        params[param_name] = sorted(values)

    if not clauses:
        return "TRUE", params
    return " AND ".join(clauses), params


def require_scope_token(statement: str) -> None:
    """Raise `ScopeTokenMissing` unless `statement` contains the literal token."""
    if SCOPE_TOKEN not in statement:
        raise ScopeTokenMissing(
            "query() requires a literal '{scope}' token in the SQL text; "
            "a query over row-level-scoped data must not omit its predicate. "
            "Add '{scope}' to the WHERE clause (e.g. 'WHERE {scope}' or "
            "'WHERE x = %(x)s AND {scope}').")


def query(session: Session, statement: str, params: Mapping[str, Any] | None = None,
          *, scope: Scope | None = None,
          columns: Mapping[str, str] | None = None) -> list[tuple]:
    """Run a `{scope}`-bearing SELECT (or `... RETURNING`) statement, scoped.

    `scope` defaults to `session.scope` -- the scope the transaction itself was
    opened with -- but can be overridden for the rare case a service needs to
    compile a *different* scope's predicate inside an already-open session
    (for example, checking whether a second principal could also see a row).
    Overriding does not touch `SET LOCAL`; it only affects the predicate
    compiled here.

    `params` must be a mapping (``%(name)s`` placeholders), never a sequence --
    this is what lets the `{scope}` token appear anywhere in `statement`
    without the caller having to reason about positional ordering against the
    scope predicate's own parameters.

    Returns whatever `session.fetchall` returns: an empty list for an
    out-of-scope or nonexistent row. Callers must treat that as "not found",
    never translate it into a 403 -- see the module docstring.
    """
    require_scope_token(statement)
    effective_scope = scope if scope is not None else session.scope
    predicate, scope_params = compile_scope(effective_scope, columns)

    if params is None:
        merged: dict[str, Any] = {}
    elif isinstance(params, Mapping):
        merged = dict(params)
    else:
        raise TypeError(
            "repo.query() requires `params` to be a mapping (named "
            "%(name)s placeholders), not a positional sequence, so the "
            "{scope} token can be substituted anywhere in the SQL safely; "
            f"got {type(params).__name__}")

    overlap = set(merged) & set(scope_params)
    if overlap:
        raise ValueError(
            f"query params collide with internal scope param names: {overlap}; "
            f"avoid the '__scope_' prefix in your own parameter names")
    merged.update(scope_params)

    final_statement = statement.replace(SCOPE_TOKEN, predicate)
    return session.fetchall(final_statement, merged)


def query_one(session: Session, statement: str, params: Mapping[str, Any] | None = None,
              *, scope: Scope | None = None,
              columns: Mapping[str, str] | None = None) -> tuple | None:
    """Convenience wrapper over `query()` returning the first row, or `None`.

    `None` covers both "no such row" and "row exists but is out of scope" --
    by design, those two cases must be indistinguishable to the caller.
    """
    rows = query(session, statement, params, scope=scope, columns=columns)
    return rows[0] if rows else None


#: The column mapping for the tables this milestone introduces, for callers
#: that want the common case without repeating the column names at every call
#: site. Not mandatory -- any caller may pass its own `columns=` mapping for a
#: joined or aliased query shape.
PROJECT_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "entity_id",
    "plant": "plant_id",
    "location": "location_id",
    # `project` was absent, so a caller scoped to specific projects -- including
    # one scoped to NO projects, frozenset() -- got TRUE instead of a filter.
    "project": "project_id",
}

WBS_ELEMENT_SCOPE_COLUMNS: dict[str, str | None] = {
    "project": "project_id",
    # wbs_element carries project_id but not the org dimensions directly; they
    # are reachable only through project. Mapped to None = waived DELIBERATELY
    # for this table, which is now a visible decision rather than an omission
    # that silently widened every query using this mapping.
    "entity": None,
    "plant": None,
    "location": None,
}
