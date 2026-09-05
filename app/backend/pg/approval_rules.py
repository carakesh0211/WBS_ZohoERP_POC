"""The approval predicate AST, route resolution, quorum and the assignee set,
plus the definition lifecycle behind Contract 3's ``/definitions`` routes.

Everything down to :func:`group_complete` is **pure logic with no database
access**. It takes plain Python data -- candidate definitions, a snapshot, role
membership, delegations, the contributor set -- and returns a decision about
how an object routes. ``approvals.py`` does the instance fetching and writing;
this module decides.

Below :func:`group_complete` is a clearly-fenced **persistence** section added
at integration: ``list_definitions``, ``create_definition``,
``activate_definition``, ``simulate`` and ``list_versions``. Those are the
CONFIGURATION half of the engine (``approval.configure``, Administrator only),
they are what ``api/approvals.py`` looks up on this module by name, and they do
no routing arithmetic of their own -- every judgement is delegated back up to
the pure half, so the tests that pin the hard parts still need no server.

That split is deliberate. The parts of the approval engine that are easy to get
wrong -- a predicate that silently coerces money through a float, a quorum that
rounds an empty approver set down to zero, a contributor filter that runs before
delegates are added instead of after -- are exactly the parts that a live
PostgreSQL is not needed to test. They are all here, and
``tests/test_pg_approvals.py`` exercises them without a database.

Specification: ``docs/WAVE4_CONTRACTS.md`` contracts 2 and 5, and
``docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md`` section 9.2.

Fail-closed is the theme
------------------------
Three separate places in section 9.2 say the same thing in different words: an
object that cannot be routed is a **configuration defect, never an approval**.

* No candidate definition matches            -> ``APPROVAL_ROUTE_UNRESOLVED``
* The approver set is empty after filtering  -> ``NO_INDEPENDENT_APPROVER``
* A quorum computes to zero                  -> refused, never "already met"

None of these is a path to auto-approval, and each is a raised exception rather
than a falsy return value, so a caller cannot reach an approved state by
forgetting to check something.
"""
from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

from psycopg.types.json import Jsonb

# ==========================================================================
# STAND-IN FOR ``app/backend/pg/approval_schema.py`` (stream 1).
#
# Stream 1 owns the schema module and migration 008. It is not present in this
# worktree, so the table names, column names and status vocabularies the engine
# needs are defined HERE, in ONE place, and imported from here by
# ``approvals.py`` and ``delegation.py``.
#
# **At integration these move to ``approval_schema.py`` and this block is
# deleted**, leaving `from .approval_schema import ...`. They are transcribed
# from docs/WAVE4_CONTRACTS.md contracts 1 and 2, which is the frozen source of
# truth for both streams, so the two should agree by construction; where they do
# not, the contract wins and this block is what changes.
#
# One name below is NOT in contract 1 and is reported to the lead as a gap
# rather than invented quietly: ``APPROVAL_ACTION_IDEMPOTENCY_KEY``. Contract 8
# requires every decision to carry an ``idempotency_key`` whose replay returns
# the original outcome, but contract 1's ``approval_action`` column list has no
# column to store it in and defines no separate table. The engine is written
# against a ``idempotency_key`` column on ``approval_action`` plus a UNIQUE
# index on ``(instance_id, idempotency_key)``; stream 1 must add both, or name
# the alternative it prefers.
# ==========================================================================

#: Tables (contract 1).
T_DEFINITION = "approval_definition"
T_RULE = "approval_rule"
T_STAGE = "approval_stage"
T_STAGE_APPROVER = "approval_stage_approver"
T_INSTANCE = "approval_instance"
T_STAGE_INSTANCE = "approval_stage_instance"
T_ASSIGNMENT = "approval_assignment"
T_ACTION = "approval_action"
T_DELEGATION = "approval_delegation"
T_REASON_CODE = "reason_code"

#: The column contract 8 needs and contract 1 does not yet declare. See above.
APPROVAL_ACTION_IDEMPOTENCY_KEY = "idempotency_key"

#: Definition lifecycle (contract 1).
DEF_DRAFT, DEF_ACTIVE, DEF_RETIRED = "DRAFT", "ACTIVE", "RETIRED"

#: Instance statuses (contract 2).
INST_OPEN = "OPEN"
INST_APPROVED = "APPROVED"
INST_REJECTED = "REJECTED"
INST_RETURNED = "RETURNED"
INST_RECALLED = "RECALLED"
INST_CANCELLED = "CANCELLED"
INST_SUPERSEDED = "SUPERSEDED"
INST_EXCEPTION_PENDING = "EXCEPTION_PENDING"
INSTANCE_STATUSES = (INST_OPEN, INST_APPROVED, INST_REJECTED, INST_RETURNED,
                     INST_RECALLED, INST_CANCELLED, INST_SUPERSEDED,
                     INST_EXCEPTION_PENDING)
#: The statuses in which an instance is still live and can receive a decision.
INSTANCE_LIVE_STATUSES = (INST_OPEN,)

#: Stage statuses (contract 2).
STAGE_PENDING = "PENDING"
STAGE_APPROVED = "APPROVED"
STAGE_REJECTED = "REJECTED"
STAGE_RETURNED = "RETURNED"
STAGE_SKIPPED = "SKIPPED"
STAGE_ESCALATED = "ESCALATED"
STAGE_STATUSES = (STAGE_PENDING, STAGE_APPROVED, STAGE_REJECTED, STAGE_RETURNED,
                  STAGE_SKIPPED, STAGE_ESCALATED)
#: A stage still accepting decisions. ESCALATED is included deliberately:
#: escalation adds an assignee, it does not close the stage.
STAGE_OPEN_STATUSES = (STAGE_PENDING, STAGE_ESCALATED)

#: Assignment states (contract 2).
ASSIGN_PENDING, ASSIGN_ACTED, ASSIGN_WITHDRAWN = "PENDING", "ACTED", "WITHDRAWN"

#: How an assignment came to exist (contract 1).
VIA_ROLE, VIA_USER, VIA_DELEGATION, VIA_ESCALATION = (
    "ROLE", "USER", "DELEGATION", "ESCALATION")

#: Approver kinds (contract 1).
APPROVER_ROLE, APPROVER_USER = "ROLE", "USER"

#: Quorum types (contract 1).
QUORUM_ALL, QUORUM_ANY, QUORUM_N_OF_M, QUORUM_PERCENT = (
    "ALL", "ANY", "N_OF_M", "PERCENT")
QUORUM_TYPES = (QUORUM_ALL, QUORUM_ANY, QUORUM_N_OF_M, QUORUM_PERCENT)

#: Decision verbs written to ``approval_action``.
ACTION_APPROVE = "APPROVE"
ACTION_REJECT = "REJECT"
ACTION_RETURN = "RETURN"
ACTION_RECALL = "RECALL"
ACTION_CANCEL = "CANCEL"
ACTION_RESUBMIT = "RESUBMIT"
ACTION_ESCALATE = "ESCALATE"
ACTION_SUPERSEDE = "SUPERSEDE"
#: The six a human takes through the decide/recall/cancel/resubmit routes.
DECISION_ACTIONS = (ACTION_APPROVE, ACTION_REJECT, ACTION_RETURN,
                    ACTION_RECALL, ACTION_CANCEL, ACTION_RESUBMIT)
#: The subset that arrives on POST /decide (contract 3).
STAGE_DECISION_ACTIONS = (ACTION_APPROVE, ACTION_REJECT, ACTION_RETURN)

#: Skip reason recorded when ``applies_when`` evaluates false (section 9.3).
SKIP_RULE_NOT_MET = "RULE_NOT_MET"

#: Error codes, frozen by contract 3.
ERR_ROUTE_UNRESOLVED = "APPROVAL_ROUTE_UNRESOLVED"
ERR_NO_INDEPENDENT_APPROVER = "NO_INDEPENDENT_APPROVER"
ERR_SELF_APPROVAL = "SELF_APPROVAL"
ERR_NOT_AN_ASSIGNEE = "NOT_AN_ASSIGNEE"
ERR_STAGE_NOT_OPEN = "STAGE_NOT_OPEN"
ERR_REASON_REQUIRED = "REASON_REQUIRED"
ERR_OBJECT_VERSION_STALE = "OBJECT_VERSION_STALE"
ERR_IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
ERR_IDEMPOTENT_REPLAY = "IDEMPOTENT_REPLAY"
ERR_DEFINITION_NOT_ACTIVE = "DEFINITION_NOT_ACTIVE"
ERR_DEFINITION_IMMUTABLE = "DEFINITION_IMMUTABLE"
ERR_DELEGATION_WINDOW_INVALID = "DELEGATION_WINDOW_INVALID"
ERR_BUDGET_MOVED = "BUDGET_MOVED"
#: Not in contract 3's list; raised for a malformed predicate at compile time,
#: which is a configuration-authoring error rather than a decision outcome.
ERR_PREDICATE_INVALID = "APPROVAL_PREDICATE_INVALID"

# ==========================================================================
# Errors
# ==========================================================================


class ApprovalError(Exception):
    """Base for every rejected approval-engine call.

    Shaped exactly like ``budget.BudgetServiceError`` -- ``code`` is the
    RFC-7807 ``code`` the API contract requires, ``status`` is the HTTP status
    the router answers with -- so the two services can be handled by one
    exception mapper and callers match on ``.code``, never on message text.
    """

    def __init__(self, code: str, message: str, *, status: int = 400,
                 detail: Mapping[str, Any] | None = None):
        self.code = code
        self.message = message
        self.status = status
        self.detail: dict[str, Any] = dict(detail or {})
        super().__init__(f"{code}: {message}")


class PredicateError(ApprovalError):
    """A predicate AST is malformed, or breaks the integer-paise rule.

    Raised at COMPILE time, never at evaluation time. A predicate that compiles
    always evaluates to a bool; a predicate that cannot be trusted never gets
    the chance to route anything.
    """

    def __init__(self, message: str):
        super().__init__(ERR_PREDICATE_INVALID, message, status=400)


class ApprovalRouteUnresolved(ApprovalError):
    """No ACTIVE definition matched. FAIL CLOSED -- never an auto-approval."""

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None):
        super().__init__(ERR_ROUTE_UNRESOLVED, message, status=409, detail=detail)


class NoIndependentApprover(ApprovalError):
    """Every candidate approver for a stage is a contributor to the object.

    Section 9.2 step 5(d): the engine does not skip the stage and does not
    approve it. The instance goes to ``EXCEPTION_PENDING`` and a human fixes
    the configuration.
    """

    def __init__(self, message: str, *, detail: Mapping[str, Any] | None = None):
        super().__init__(ERR_NO_INDEPENDENT_APPROVER, message, status=409,
                          detail=detail)


# ==========================================================================
# Snapshot paths
# ==========================================================================

#: Returned by :func:`resolve_path` when a dotted path does not exist in the
#: snapshot. A module-level singleton so identity comparison (`is`) is the test,
#: and so it can never be confused with a legitimate ``None`` in the data --
#: "the field is absent" and "the field is null" are different, and a predicate
#: written against one must not silently match the other.
class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:          # pragma: no cover - debugging aid
        return "<MISSING>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()

#: A path whose LAST segment ends with this is a monetary path, and the
#: integer-paise rule below applies to anything compared against it. The suffix
#: is the naming convention the whole product already uses (`delta_paise`,
#: `amount_paise`, `available_paise`), so the rule needs no separate registry
#: of "which fields are money" that could drift out of date.
MONEY_SUFFIX = "_paise"

#: The snapshot dimensions section 9.2's conditions must be able to reach.
#: Not enforced -- a predicate may read any path -- but documented here and
#: asserted by the tests, so "conditions cover amount, entity, plant, project,
#: location, category, budget head and document type" is a checkable claim
#: rather than a hope. See :func:`covered_condition_dimensions`.
CONDITION_DIMENSIONS: dict[str, str] = {
    "amount": "amount_paise",
    "entity": "entity_id",
    "plant": "plant_id",
    "project": "project_id",
    "location": "location_id",
    "category": "asset_category",
    "budget_head": "budget_head_id",
    "document_type": "object_type",
}


def is_money_path(path: str) -> bool:
    """True when `path`'s final segment names an integer-paise field."""
    return path.rsplit(".", 1)[-1].endswith(MONEY_SUFFIX)


def resolve_path(snapshot: Any, path: str) -> Any:
    """Walk a dotted path through nested mappings. Returns MISSING if absent.

    **Mappings only.** A segment applied to anything that is not a `Mapping`
    yields MISSING rather than reaching for an attribute. That is the structural
    reason this evaluator cannot perform arbitrary attribute access: there is no
    code path from a predicate to `getattr`, so a predicate cannot read
    ``__class__`` or ``__globals__`` no matter what string it contains. Section
    9.2: "No user code, no regex, no arbitrary attribute access."
    """
    current: Any = snapshot
    for segment in path.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return MISSING
        current = current[segment]
    return current


def covered_condition_dimensions(predicate: Mapping[str, Any] | bool) -> set[str]:
    """Which of :data:`CONDITION_DIMENSIONS` this predicate actually reads.

    Used by the tests to prove the AST can express every condition section 9.2
    requires, and available to an activation-time validator that wants to warn
    about a definition routing on nothing.
    """
    paths = _paths_in(predicate)
    return {name for name, field_name in CONDITION_DIMENSIONS.items()
            if any(p == field_name or p.endswith("." + field_name) for p in paths)}


def _paths_in(node: Any) -> set[str]:
    if isinstance(node, Mapping):
        found: set[str] = set()
        if "path" in node and isinstance(node.get("path"), str):
            found.add(node["path"])
        for value in node.values():
            found |= _paths_in(value)
        return found
    if isinstance(node, (list, tuple)):
        out: set[str] = set()
        for item in node:
            out |= _paths_in(item)
        return out
    return set()


# ==========================================================================
# The predicate AST
# ==========================================================================

#: Boolean connectives.
_LOGICAL_OPS = ("and", "or", "not")
#: Binary comparisons. Every one is total: it returns a bool for any pair of
#: operand values, including MISSING and including mismatched types.
_COMPARE_OPS = ("==", "!=", "<", "<=", ">", ">=", "in", "not_in")
#: The constant predicates. Section 9.3's worked example needs a `true`
#: catch-all rule (`p30 true -> STANDARD`); `false` exists for symmetry and for
#: temporarily disabling a rule without deleting it.
_CONST_OPS = ("true", "false")

OPERATORS = _LOGICAL_OPS + _COMPARE_OPS + _CONST_OPS

#: Literal types a predicate may carry. `float` is present because a non-money
#: comparison (a percentage threshold, say) legitimately needs one; the money
#: rule below is what keeps a float away from a paise field. `Decimal` is
#: absent: it is not JSON, so it cannot appear in a `jsonb` predicate column.
_LITERAL_TYPES = (str, int, float, bool, type(None))


@dataclass(frozen=True)
class _Operand:
    """One side of a comparison: either a snapshot path or a literal."""

    path: str | None
    literal: Any
    is_path: bool

    def value(self, snapshot: Any) -> Any:
        if self.is_path:
            assert self.path is not None      # guaranteed by _compile_operand
            return resolve_path(snapshot, self.path)
        return self.literal

    def describe(self) -> str:
        return f"path {self.path!r}" if self.is_path else f"literal {self.literal!r}"


@dataclass(frozen=True)
class CompiledPredicate:
    """A validated predicate, ready to evaluate against any snapshot.

    Instances are produced only by :func:`compile_predicate`, so holding one is
    proof the AST passed validation -- including the integer-paise rule.
    """

    op: str
    args: tuple["CompiledPredicate", ...] = ()
    left: _Operand | None = None
    right: _Operand | None = None
    source: Any = None

    def evaluate(self, snapshot: Any) -> bool:
        """Total, side-effect free, always returns a bool."""
        if self.op == "true":
            return True
        if self.op == "false":
            return False
        if self.op == "and":
            return all(arg.evaluate(snapshot) for arg in self.args)
        if self.op == "or":
            return any(arg.evaluate(snapshot) for arg in self.args)
        if self.op == "not":
            return not self.args[0].evaluate(snapshot)

        assert self.left is not None and self.right is not None
        return _compare(self.op, self.left.value(snapshot), self.right.value(snapshot))

    # A predicate is asked "does this match?" often enough that the alias reads
    # better at the call sites in approvals.py.
    __call__ = evaluate


def _compare(op: str, left: Any, right: Any) -> bool:
    """Evaluate one comparison. **Never raises, always returns a bool.**

    Two cases are resolved to ``False`` rather than to an exception or to a
    surprising truth:

    * **An operand is MISSING.** The leaf is False, whichever operator it is --
      including ``!=`` and ``not_in``, where "the field is absent, so it differs
      from what I named" is a defensible reading that happens to be the
      dangerous one. A rule must match on what a document SAYS, not on what it
      omits, or a snapshot missing a field routes itself down a cheaper path.
      A ``not`` wrapped around such a leaf still inverts it to True; the
      activation-time validator is where "this rule reads a path no snapshot
      class provides" belongs, and it is recorded as an open gap below.
    * **The types do not order.** ``"HIGH" < 5`` raises `TypeError` in Python 3.
      A misconfigured comparison must not turn into a 500 in the middle of
      routing a purchase order, so it is False and the next rule gets its turn.
    """
    if left is MISSING or right is MISSING:
        return False

    if op == "in" or op == "not_in":
        # Membership needs a container, and a bare string is a container of
        # characters -- `'LAND' in 'HIGHLANDS'` is True and means nothing here.
        if isinstance(right, (str, bytes)) or not isinstance(right, (Sequence, set, frozenset)):
            return False
        contained = any(_eq(left, item) for item in right)
        return contained if op == "in" else not contained

    if op == "==":
        return _eq(left, right)
    if op == "!=":
        return not _eq(left, right)

    try:
        if op == "<":
            return bool(left < right)
        if op == "<=":
            return bool(left <= right)
        if op == ">":
            return bool(left > right)
        if op == ">=":
            return bool(left >= right)
    except TypeError:
        return False
    raise AssertionError(f"unreachable operator {op!r}")   # pragma: no cover


def _eq(left: Any, right: Any) -> bool:
    """Equality that does not conflate a bool with the integer beside it.

    ``True == 1`` is true in Python. A predicate comparing a boolean flag
    against an amount, or an amount against ``True``, is a configuration error;
    matching it would be worse than not matching it.
    """
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return bool(left == right)


def compile_predicate(node: Any, *, _depth: int = 0) -> CompiledPredicate:
    """Validate and compile a predicate AST. Raises :class:`PredicateError`.

    Accepted shapes::

        true | false                                  (bare JSON booleans)
        {"op": "true"} | {"op": "false"}
        {"op": "and"|"or", "args": [pred, ...]}       (one or more)
        {"op": "not",      "args": [pred]}            (exactly one)
        {"op": <compare>,  "left": operand, "right": operand}

        operand := {"path": "dotted.snapshot.path"} | {"value": <literal>}

    Everything else is refused: an unknown operator, a missing operand, an
    operand that is both a path and a value, a literal that is not JSON-scalar,
    a `not` with two arguments. There is no escape hatch, no callable operand
    and no expression string, because a predicate is *configuration* supplied by
    an administrator and must not be able to become *code*.

    **The integer-paise rule.** ``money.to_paise`` refuses a float that is not
    already an exact two-decimal amount, for the reasons in its docstring: a
    float cannot represent most money exactly, and rounding one silently is how
    the original defect happened. The same reasoning applies here with more
    force, because a threshold in a routing rule decides *who has to approve*.
    So any literal compared against a path whose name ends ``_paise`` must be an
    `int`; a `float` -- even one as innocent as ``5000000.0`` -- is refused at
    compile time, and so is a numeric string. Applies through `in`/`not_in`
    lists element by element.
    """
    if _depth > 64:
        raise PredicateError(
            "predicate nests deeper than 64 levels; this is a configuration "
            "error, and evaluating it risks exhausting the stack.")

    # A bare JSON boolean is the natural way to write a catch-all rule.
    if isinstance(node, bool):
        return CompiledPredicate(op="true" if node else "false", source=node)

    if not isinstance(node, Mapping):
        raise PredicateError(
            f"a predicate must be a JSON object or a bare boolean, got "
            f"{type(node).__name__}: {node!r}")

    if "op" not in node:
        raise PredicateError(f"predicate has no 'op' key: {dict(node)!r}")
    op = node["op"]
    if not isinstance(op, str) or op not in OPERATORS:
        raise PredicateError(
            f"unknown predicate operator {op!r}. Permitted: "
            f"{', '.join(OPERATORS)}.")

    unexpected = set(node) - {"op", "args", "left", "right"}
    if unexpected:
        raise PredicateError(
            f"predicate for operator {op!r} carries unexpected key(s) "
            f"{sorted(unexpected)!r}; only 'args', 'left' and 'right' are read, "
            f"and an ignored key is usually a misspelling that silently changes "
            f"which documents match.")

    if op in _CONST_OPS:
        if node.get("args") or node.get("left") is not None or node.get("right") is not None:
            raise PredicateError(f"the constant predicate {op!r} takes no operands.")
        return CompiledPredicate(op=op, source=node)

    if op in _LOGICAL_OPS:
        args = node.get("args")
        if not isinstance(args, (list, tuple)) or not args:
            raise PredicateError(
                f"{op!r} needs a non-empty 'args' list, got {args!r}. An empty "
                f"'and' is vacuously true and an empty 'or' vacuously false; "
                f"both are almost certainly a mistake, so neither is accepted.")
        if op == "not" and len(args) != 1:
            raise PredicateError(f"'not' takes exactly one argument, got {len(args)}.")
        compiled = tuple(compile_predicate(arg, _depth=_depth + 1) for arg in args)
        return CompiledPredicate(op=op, args=compiled, source=node)

    # A comparison.
    if "left" not in node or "right" not in node:
        raise PredicateError(
            f"comparison {op!r} needs both 'left' and 'right' operands; got "
            f"keys {sorted(node)!r}.")
    left = _compile_operand(node["left"], side="left", op=op)
    right = _compile_operand(node["right"], side="right", op=op, allow_list=(op in ("in", "not_in")))

    if op in ("in", "not_in") and not right.is_path and not isinstance(right.literal, (list, tuple)):
        raise PredicateError(
            f"{op!r} needs a list on the right, or a path resolving to one; got "
            f"{right.describe()}.")

    _assert_money_discipline(op, left, right)
    return CompiledPredicate(op=op, left=left, right=right, source=node)


def _compile_operand(node: Any, *, side: str, op: str,
                      allow_list: bool = False) -> _Operand:
    if not isinstance(node, Mapping):
        raise PredicateError(
            f"the {side} operand of {op!r} must be an object -- either "
            f"{{'path': ...}} or {{'value': ...}} -- got {node!r}. A bare "
            f"literal is refused so that 'is this a field name or a string?' "
            f"is never a guess.")
    has_path, has_value = "path" in node, "value" in node
    if has_path == has_value:
        raise PredicateError(
            f"the {side} operand of {op!r} must carry exactly one of 'path' or "
            f"'value'; got {sorted(node)!r}.")
    extra = set(node) - {"path", "value"}
    if extra:
        raise PredicateError(
            f"the {side} operand of {op!r} carries unexpected key(s) {sorted(extra)!r}.")

    if has_path:
        path = node["path"]
        if not isinstance(path, str) or not path.strip():
            raise PredicateError(
                f"the {side} operand of {op!r} has a non-string or empty path: {path!r}.")
        segments = path.split(".")
        if any(not segment.strip() for segment in segments):
            raise PredicateError(
                f"path {path!r} has an empty segment; a dotted path must name a "
                f"field at every level.")
        if any(segment.startswith("__") for segment in segments):
            raise PredicateError(
                f"path {path!r} contains a dunder segment. Paths index mappings "
                f"and never touch attributes, so this could not have reached "
                f"one, but a predicate asking for it is refused rather than "
                f"quietly resolved to MISSING.")
        return _Operand(path=path, literal=None, is_path=True)

    literal = node["value"]
    _assert_literal(literal, side=side, op=op, allow_list=allow_list)
    return _Operand(path=None, literal=literal, is_path=False)


def _assert_literal(value: Any, *, side: str, op: str, allow_list: bool) -> None:
    if isinstance(value, (list, tuple)):
        if not allow_list:
            raise PredicateError(
                f"the {side} operand of {op!r} may not be a list; only 'in' and "
                f"'not_in' take one.")
        for item in value:
            if not isinstance(item, _LITERAL_TYPES):
                raise PredicateError(
                    f"list literal for {op!r} contains a {type(item).__name__}; "
                    f"only JSON scalars are permitted.")
        return
    if not isinstance(value, _LITERAL_TYPES):
        raise PredicateError(
            f"the {side} operand of {op!r} is a {type(value).__name__}; a "
            f"literal must be a JSON scalar (string, number, boolean or null).")


def _assert_money_discipline(op: str, left: _Operand, right: _Operand) -> None:
    """Refuse a non-integer literal compared against a ``*_paise`` path.

    This is the compiler half of the promise ``money.py`` makes at the API
    boundary. There, a float amount that is not an exact 2dp value is rejected
    so the caller sends a decimal string. Here, the value being compared is
    already IN paise -- an integer count of the smallest unit -- so there is no
    exact float to accept and no rounding decision to make. Any float is wrong,
    ``5000000.0`` included, and so is ``"5000000"``: a numeric string orders
    lexicographically, which would make ``amount_paise > "9"`` false for every
    amount up to ten crore.
    """
    for path_side, literal_side in ((left, right), (right, left)):
        if not (path_side.is_path and path_side.path and is_money_path(path_side.path)):
            continue
        if literal_side.is_path:
            continue                      # path vs path: both are paise columns
        items = (literal_side.literal if isinstance(literal_side.literal, (list, tuple))
                 else [literal_side.literal])
        for item in items:
            if isinstance(item, bool) or not isinstance(item, int):
                raise PredicateError(
                    f"{path_side.path!r} is an integer-paise field, so it may "
                    f"only be compared against an integer number of paise; got "
                    f"{type(item).__name__} {item!r}. Money is never a float in "
                    f"this application (see app/backend/money.py): a float "
                    f"cannot represent most rupee amounts exactly, and a routing "
                    f"threshold decides who must approve. Write 5000000 for "
                    f"Rs 50,000.00, not 50000.0 and not '5000000'.")


# ==========================================================================
# Route resolution (section 9.2 steps 1-3)
# ==========================================================================


@dataclass(frozen=True)
class RuleSpec:
    """One ``approval_rule`` row, already compiled."""

    priority: int
    predicate: CompiledPredicate
    rule_id: str | None = None


@dataclass(frozen=True)
class DefinitionSpec:
    """One ``approval_definition`` row with its rules."""

    definition_id: str
    object_type: str
    code: str
    version: int
    status: str
    entity_id: str | None
    effective_from: date | None
    effective_to: date | None
    rules: tuple[RuleSpec, ...]

    @property
    def is_entity_specific(self) -> bool:
        return self.entity_id is not None


@dataclass(frozen=True)
class Resolution:
    """Which definition routed the object, and on which rule."""

    definition: DefinitionSpec
    rule: RuleSpec
    considered: tuple[str, ...] = ()


def _as_date(value: Any, *, label: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ApprovalError(
                ERR_ROUTE_UNRESOLVED,
                f"{label} is not an ISO date: {value!r}", status=400) from exc
    raise ApprovalError(ERR_ROUTE_UNRESOLVED,
                         f"{label} is not a date: {value!r}", status=400)


def build_definition(row: Mapping[str, Any]) -> DefinitionSpec:
    """Compile one definition (and its rules) from a plain mapping.

    Duplicate priorities are refused here rather than at activation only: a
    definition whose rules cannot be totally ordered has no defined "first
    matching rule", and section 9.2 step 2 depends on that order being a fact.
    """
    rules_in = list(row.get("rules") or [])
    seen_priorities: set[int] = set()
    rules: list[RuleSpec] = []
    for raw in rules_in:
        priority = raw["priority"]
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise PredicateError(
                f"rule priority must be an integer, got {priority!r} in "
                f"definition {row.get('definition_id')!r}.")
        if priority in seen_priorities:
            raise PredicateError(
                f"definition {row.get('definition_id')!r} has two rules at "
                f"priority {priority}. 'First matching rule, priority ASC' has "
                f"no meaning when the order is ambiguous, and UNIQUE "
                f"(definition_id, priority) forbids it in the database.")
        seen_priorities.add(priority)
        rules.append(RuleSpec(priority=priority,
                              predicate=compile_predicate(raw["predicate"]),
                              rule_id=raw.get("rule_id")))
    rules.sort(key=lambda r: r.priority)

    return DefinitionSpec(
        definition_id=row["definition_id"],
        object_type=row["object_type"],
        code=row["code"],
        version=int(row["version"]),
        status=row["status"],
        entity_id=row.get("entity_id"),
        effective_from=_as_date(row.get("effective_from"), label="effective_from"),
        effective_to=_as_date(row.get("effective_to"), label="effective_to"),
        rules=tuple(rules),
    )


def candidate_definitions(definitions: Iterable[DefinitionSpec], *, object_type: str,
                           business_date: date, entity_id: str | None) -> list[DefinitionSpec]:
    """Section 9.2 step 1, including the ordering step 1 ends with.

    Kept: ``ACTIVE``, matching ``object_type``, ``business_date`` inside
    ``[effective_from, effective_to]`` (either bound absent meaning open), and
    ``entity_id`` either equal to the object's or NULL (a global definition).

    Ordered: **entity-specific before global**, then by ``code`` and by
    descending ``version``. The last two are not in the specification and are
    not a policy choice -- they are there so that two definitions the
    specification considers equally eligible resolve the same way on every run,
    on every machine, rather than in whatever order the database returned them.
    A route that depends on row order is a route nobody can reproduce.

    ``effective_to`` is INCLUSIVE. A definition effective to 2026-03-31 routes
    an object dated 2026-03-31; an exclusive reading would leave the last day of
    a fiscal year unroutable, which fails closed but for no good reason.
    """
    kept = []
    for definition in definitions:
        if definition.status != DEF_ACTIVE:
            continue
        if definition.object_type != object_type:
            continue
        if definition.effective_from is not None and business_date < definition.effective_from:
            continue
        if definition.effective_to is not None and business_date > definition.effective_to:
            continue
        if definition.entity_id is not None and definition.entity_id != entity_id:
            continue
        kept.append(definition)

    kept.sort(key=lambda d: (0 if d.is_entity_specific else 1, d.code, -d.version))
    return kept


def resolve_route(definitions: Iterable[DefinitionSpec], snapshot: Mapping[str, Any], *,
                   object_type: str, business_date: date,
                   entity_id: str | None = None) -> Resolution:
    """Section 9.2 steps 1-3. Raises :class:`ApprovalRouteUnresolved` on no match.

    "First definition whose first matching rule (priority ASC) evaluates true
    wins": the definitions are walked in the order :func:`candidate_definitions`
    established, and within each, its rules in ascending priority. The first
    rule that evaluates true ends the search -- both loops, not just the inner
    one -- and a definition with no matching rule is passed over entirely rather
    than contributing a default.

    A definition with **no rules at all** matches nothing. It is not a catch-all:
    a catch-all is written explicitly as a ``true`` rule at the highest priority
    number, so that "everything routes here" is something somebody typed.
    """
    if entity_id is None:
        resolved = resolve_path(snapshot, CONDITION_DIMENSIONS["entity"])
        entity_id = None if resolved is MISSING else resolved

    candidates = candidate_definitions(definitions, object_type=object_type,
                                        business_date=business_date, entity_id=entity_id)
    considered: list[str] = []
    for definition in candidates:
        considered.append(definition.definition_id)
        for rule in definition.rules:                 # already priority ASC
            if rule.predicate.evaluate(snapshot):
                return Resolution(definition=definition, rule=rule,
                                   considered=tuple(considered))

    raise ApprovalRouteUnresolved(
        f"No ACTIVE approval definition routes a {object_type!r} dated "
        f"{business_date.isoformat()} for entity {entity_id!r}. "
        f"{len(candidates)} definition(s) were eligible and none had a matching "
        f"rule. This is a configuration defect: the object is held in "
        f"EXCEPTION_PENDING for an administrator, and is NEVER auto-approved.",
        detail={"object_type": object_type,
                "business_date": business_date.isoformat(),
                "entity_id": entity_id,
                "candidates_considered": considered})


# ==========================================================================
# Stages (section 9.2 step 4)
# ==========================================================================


@dataclass(frozen=True)
class StageSpec:
    """One ``approval_stage`` row with its approver references."""

    stage_no: int
    name: str
    quorum_type: str
    quorum_n: int | None = None
    parallel_group: str | None = None
    applies_when: CompiledPredicate | None = None
    sla_hours: int | None = None
    escalate_after_hours: int | None = None
    escalate_to: Mapping[str, Any] | None = None
    allow_delegation: bool = True
    requires_reason: bool = False
    reason_code_set: str | None = None
    approvers: tuple[Mapping[str, Any], ...] = ()
    stage_id: str | None = None


def build_stage(row: Mapping[str, Any]) -> StageSpec:
    applies_when_raw = row.get("applies_when")
    quorum_type = row.get("quorum_type", QUORUM_ALL)
    if quorum_type not in QUORUM_TYPES:
        raise PredicateError(
            f"stage {row.get('stage_no')!r} has quorum_type {quorum_type!r}; "
            f"permitted: {', '.join(QUORUM_TYPES)}.")
    return StageSpec(
        stage_no=int(row["stage_no"]),
        name=row.get("name") or f"Stage {row['stage_no']}",
        quorum_type=quorum_type,
        quorum_n=row.get("quorum_n"),
        parallel_group=row.get("parallel_group"),
        applies_when=(None if applies_when_raw is None
                      else compile_predicate(applies_when_raw)),
        sla_hours=row.get("sla_hours"),
        escalate_after_hours=row.get("escalate_after_hours"),
        escalate_to=row.get("escalate_to"),
        allow_delegation=bool(row.get("allow_delegation", True)),
        requires_reason=bool(row.get("requires_reason", False)),
        reason_code_set=row.get("reason_code_set"),
        approvers=tuple(row.get("approvers") or ()),
        stage_id=row.get("stage_id"),
    )


def stage_applies(stage: StageSpec, snapshot: Mapping[str, Any]) -> tuple[bool, str | None]:
    """``(applies, skip_reason)``. A stage with no ``applies_when`` always applies.

    Section 9.2 step 4 and contract 2: a stage that does not apply is **recorded
    SKIPPED with a skip_reason, never omitted**, because an auditor reading the
    trail must be able to see what did not run and why. The caller writes the
    row; this function supplies the verdict and the reason.
    """
    if stage.applies_when is None:
        return True, None
    if stage.applies_when.evaluate(snapshot):
        return True, None
    return False, SKIP_RULE_NOT_MET


# ==========================================================================
# Assignees (section 9.2 step 5) and quorum (step 6)
# ==========================================================================


@dataclass(frozen=True)
class Assignee:
    """One resolved approver, and how they came to be one."""

    user_id: str
    assigned_via: str
    delegated_from: str | None = None


@dataclass(frozen=True)
class AssigneeSet:
    """The outcome of step 5, including what was removed and why.

    ``excluded_contributors`` is carried rather than discarded so the
    ``NO_INDEPENDENT_APPROVER`` exception, and the SCR-25 screen behind it, can
    say *which* people were removed. "There is nobody to approve this" is not an
    actionable message; "the only Procurement holder in scope is U-PROC, who
    raised it" is.
    """

    assignees: tuple[Assignee, ...]
    excluded_contributors: tuple[str, ...] = ()
    candidates_before_filter: tuple[str, ...] = ()

    @property
    def user_ids(self) -> tuple[str, ...]:
        return tuple(a.user_id for a in self.assignees)

    def __len__(self) -> int:
        return len(self.assignees)


def scope_matches(scope_expr: Mapping[str, Any] | None,
                   snapshot: Mapping[str, Any],
                   member_scope: Mapping[str, Any] | None) -> bool:
    """Does a role holder's own scope satisfy a stage approver's ``scope_expr``?

    ``scope_expr`` is a mapping of dimension -> snapshot path, for example
    ``{"project": "project_id"}``: read the object's project from the snapshot,
    and keep only role holders granted that project. A holder whose grant for
    that dimension is absent or ``None`` is **unrestricted on it** and matches
    anything -- the same "no restriction row means unrestricted" convention
    ``roles.resolve_scope`` already uses, so the two layers cannot disagree
    about what an absent grant means.

    An empty or absent ``scope_expr`` places no restriction: every holder of the
    role is a candidate.
    """
    if not scope_expr:
        return True
    for dimension, path in scope_expr.items():
        wanted = resolve_path(snapshot, path) if isinstance(path, str) else path
        if wanted is MISSING or wanted is None:
            # The object does not carry this dimension, so it cannot be used to
            # narrow the role. Fail OPEN here on purpose: narrowing to nobody
            # would manufacture a NO_INDEPENDENT_APPROVER out of a snapshot gap,
            # and the empty-set check below is where a genuinely empty role is
            # caught and refused.
            continue
        granted = (member_scope or {}).get(dimension)
        if granted is None:
            continue                                  # unrestricted on this dimension
        if isinstance(granted, (str, bytes)):
            granted = [granted]
        if wanted not in set(granted):
            return False
    return True


def expand_role_members(stage: StageSpec, snapshot: Mapping[str, Any],
                         role_members: Mapping[str, Iterable[str]],
                         member_scopes: Mapping[str, Mapping[str, Any]] | None = None
                         ) -> list[Assignee]:
    """Section 9.2 step 5(a): expand ROLE refs against role_grant + scope_expr.

    ``role_members`` maps a role name to the users holding it (the caller has
    already read ``role_grant``); ``member_scopes`` maps a user to their granted
    scope (from ``user_scope_restriction``/``user_scope_grant``). A USER approver
    reference is taken literally and is not scope-filtered -- naming a specific
    person IS the scoping decision.

    Order is stable: approver references in ``ordinal`` order, and the users
    within each role sorted, so the assignee list for a given object is the same
    list every time it is computed. Reproducibility is worth more here than any
    ordering cleverness.
    """
    out: list[Assignee] = []
    seen: set[str] = set()
    for approver in stage.approvers:
        kind = approver.get("approver_kind")
        ref = approver.get("approver_ref")
        if kind == APPROVER_USER:
            if isinstance(ref, str) and ref and ref not in seen:
                seen.add(ref)
                out.append(Assignee(user_id=ref, assigned_via=VIA_USER))
            continue
        if kind != APPROVER_ROLE:
            raise PredicateError(
                f"stage {stage.stage_no} has approver_kind {kind!r}; this engine "
                f"resolves {APPROVER_ROLE!r} and {APPROVER_USER!r} only. "
                f"(Section 9.1 also lists ATTRIBUTE; contract 1 does not, and an "
                f"unimplemented kind is refused rather than treated as nobody, "
                f"which would silently empty a stage.)")
        scope_expr = approver.get("scope_expr")
        for user_id in sorted(set(role_members.get(ref, ()) or ())):
            if user_id in seen:
                continue
            if not scope_matches(scope_expr, snapshot, (member_scopes or {}).get(user_id)):
                continue
            seen.add(user_id)
            out.append(Assignee(user_id=user_id, assigned_via=VIA_ROLE))
    return out


def apply_contributor_filter(candidates: Iterable[Assignee],
                              contributors: Iterable[str]) -> AssigneeSet:
    """Section 9.2 step 5(c)/(d): remove every contributor; refuse an empty set.

    Runs **after** delegates have been added, never before. The order matters
    and is not interchangeable: a delegate of a contributor is themselves barred
    only if they are also a contributor, and a contributor's delegate must not
    inherit the contributor's exclusion -- but equally, a delegate who happens to
    be a contributor must not slip in through the delegation route. Filtering
    last is what makes both true, because the filter sees the final set.

    Raises :class:`NoIndependentApprover` on empty. It does not return an empty
    set for a caller to interpret, because the one interpretation that must
    never happen -- "nobody needs to approve, so it is approved" -- is exactly
    what a falsy return value invites.
    """
    contributor_set_ = {c for c in contributors if c}
    candidates = list(candidates)
    before = tuple(a.user_id for a in candidates)
    kept = tuple(a for a in candidates if a.user_id not in contributor_set_)
    removed = tuple(sorted({a.user_id for a in candidates} & contributor_set_))

    if not kept:
        raise NoIndependentApprover(
            "Every candidate approver for this stage contributed to the object "
            f"({', '.join(removed) or 'the candidate set was empty to begin with'}), "
            "so no independent approver remains. The stage is NOT skipped and is "
            "NOT auto-approved: the instance goes to EXCEPTION_PENDING for an "
            "administrator to widen the role, the scope or the delegation.",
            detail={"candidates": list(before), "excluded_contributors": list(removed)})

    return AssigneeSet(assignees=kept, excluded_contributors=removed,
                        candidates_before_filter=before)


def required_quorum(quorum_type: str, quorum_n: int | None, assignee_count: int) -> int:
    """Section 9.2 step 6. Returns how many approvals the stage needs.

    ==========  ====================================================
    ``ALL``     every assignee
    ``ANY``     1
    ``N_OF_M``  ``quorum_n``, which must be between 1 and the count
    ``PERCENT`` ``ceil(quorum_n% x count)``, at least 1
    ==========  ====================================================

    ``assignee_count == 0`` raises rather than returning 0. A zero requirement is
    a met requirement, and "the stage needs zero approvals" is the arithmetic
    form of auto-approval -- the single outcome contract 2 forbids. The empty set
    is caught earlier by :func:`apply_contributor_filter`; this is the second,
    independent place it cannot get through.

    ``PERCENT`` is computed with integer arithmetic (``ceil(n*count/100)`` over
    ints, not ``n/100.0 * count``) so that 3 of 3 at 100% is 3 and not 2 -- the
    kind of off-by-one a float introduces at exactly the values a percentage
    quorum is usually configured with.
    """
    if assignee_count <= 0:
        raise NoIndependentApprover(
            "A quorum was requested for a stage with no assignees. A stage with "
            "an empty approver set is never satisfied and never auto-approved; "
            "it is an EXCEPTION_PENDING configuration defect.",
            detail={"quorum_type": quorum_type, "quorum_n": quorum_n})

    if quorum_type == QUORUM_ALL:
        return assignee_count
    if quorum_type == QUORUM_ANY:
        return 1
    if quorum_type == QUORUM_N_OF_M:
        if not isinstance(quorum_n, int) or isinstance(quorum_n, bool) or quorum_n < 1:
            raise PredicateError(
                f"N_OF_M needs a quorum_n of at least 1, got {quorum_n!r}.")
        if quorum_n > assignee_count:
            raise PredicateError(
                f"N_OF_M requires {quorum_n} approvals but the stage has only "
                f"{assignee_count} assignee(s); the stage could never complete. "
                f"Activation-time validation exists to catch this before a "
                f"definition goes ACTIVE.")
        return quorum_n
    if quorum_type == QUORUM_PERCENT:
        if not isinstance(quorum_n, int) or isinstance(quorum_n, bool) or not (0 < quorum_n <= 100):
            raise PredicateError(
                f"PERCENT needs a quorum_n percentage in 1..100, got {quorum_n!r}.")
        needed = -((-quorum_n * assignee_count) // 100)     # integer ceil
        return max(1, min(assignee_count, needed))
    raise PredicateError(f"unknown quorum_type {quorum_type!r}.")


def quorum_met(required: int, approvals: int) -> bool:
    """True only when a POSITIVE requirement has been met.

    ``required <= 0`` is False, not True. Reaching this function with a
    non-positive requirement means something upstream failed open, and the
    honest answer to "is the quorum met?" in that state is no.
    """
    return required > 0 and approvals >= required


def group_complete(stage_states: Iterable[Mapping[str, Any]]) -> bool:
    """Is a parallel group finished?

    Contract/section 9.3: stages sharing a ``parallel_group`` open together and
    the group completes when **every** stage in it meets quorum, **in either
    order**. Order-independence is the whole point, so this is a conjunction over
    the group with no notion of which stage went first, and a group containing no
    stages is not complete (there is nothing to have completed).
    """
    states = list(stage_states)
    if not states:
        return False
    return all(
        state.get("status") == STAGE_APPROVED
        or quorum_met(int(state.get("quorum_required") or 0),
                      int(state.get("quorum_met") or 0))
        for state in states)


# ==========================================================================
# Persistence: the definition lifecycle behind Contract 3's five
# ``/api/approvals/definitions...`` routes.
#
# Everything ABOVE this line is pure logic and needs no database; everything
# below it needs one. The split is narrowed rather than abandoned -- the
# functions here do no routing arithmetic of their own. They read and write
# rows and delegate every judgement to ``build_definition``, ``build_stage``,
# ``compile_predicate``, ``candidate_definitions`` and ``required_quorum``
# above, so the tests that pin the hard parts still need no server.
#
# They live in this module rather than in ``approvals.py`` because they are
# the CONFIGURATION half of the engine (``approval.configure``, Administrator
# only) and ``approvals.py`` is the INSTANCE half. ``api/approvals.py`` looks
# all five up on this module by name, which is the seam Contract 3 declares.
#
# Contract 1's immutability is the DATABASE's, not this module's: migration
# 008 carries ``assert_approval_definition_immutable()`` and its child
# trigger, so editing an ACTIVE or RETIRED definition raises SQLSTATE 42501
# whatever a Python caller intended. What is written here is the readable
# ``DEFINITION_IMMUTABLE`` in front of that -- never the only line of defence.
# ==========================================================================

_DEFINITION_COLUMNS = (
    "definition_id, object_type, code, version, status, entity_id, "
    "effective_from, effective_to, created_at, created_by, activated_at, "
    "activated_by, retired_at, retired_by"
)

_DEFINITION_KEYS = (
    "definition_id", "object_type", "code", "version", "status", "entity_id",
    "effective_from", "effective_to", "created_at", "created_by",
    "activated_at", "activated_by", "retired_at", "retired_by",
)

#: Contract 6, for the definition list. ``approval_definition`` carries only
#: ``entity_id``; the other three dimensions have no column on this table and
#: are waived EXPLICITLY (mapped to ``None``) rather than omitted, because
#: ``repo.compile_scope`` refuses a restricted dimension a caller merely forgot
#: and silently widening is the failure that rule exists to prevent.
_DEFINITION_SCOPE_COLUMNS: dict[str, str | None] = {
    "entity": "d.entity_id",
    "project": None,
    "plant": None,
    "location": None,
}

#: Page-size ceiling, mirroring ``api/approvals.py``'s own cap so a caller that
#: reaches the engine directly cannot ask for an unbounded page.
MAX_PAGE = 200
DEFAULT_PAGE = 50


def _page_size(limit: int | None) -> int:
    try:
        value = int(limit) if limit is not None else DEFAULT_PAGE
    except (TypeError, ValueError):
        value = DEFAULT_PAGE
    return min(max(value, 1), MAX_PAGE)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def paginate(items: list[dict[str, Any]], limit: int, cursor_of) -> dict[str, Any]:
    """The cursor-paginated envelope Contract 3 requires on EVERY list.

    ``items`` is one row longer than the page when a next page exists -- every
    caller below fetches ``limit + 1`` -- so "is there more" is answered by a
    fact rather than by comparing a returned count against the page size, which
    is wrong on the exact-multiple boundary and reports a spurious extra page.

    The cursor produced here is PLAIN text. ``api/approvals.py`` base64-encodes
    it on the way out and decodes it on the way back in, so the value a client
    holds is opaque to the client and never composed by one.
    """
    has_more = len(items) > limit
    page = items[:limit]
    next_cursor = cursor_of(page[-1]) if (has_more and page) else None
    return {"items": page, "next_cursor": next_cursor, "has_more": has_more}


def _definition_row(row: Sequence[Any]) -> dict[str, Any]:
    out = dict(zip(_DEFINITION_KEYS, row))
    for key in ("effective_from", "effective_to", "created_at", "activated_at",
                "retired_at"):
        out[key] = _iso(out[key])
    return out


def load_definition_rules(session, definition_id: str) -> list[dict[str, Any]]:
    """Every rule of one definition, priority ascending."""
    rows = session.fetchall(
        "SELECT rule_id, priority, predicate, description FROM approval_rule "
        "WHERE definition_id = %s ORDER BY priority",
        (definition_id,))
    return [{"rule_id": r[0], "priority": r[1], "predicate": r[2],
             "description": r[3]} for r in rows]


def load_definition_stages(session, definition_id: str) -> list[dict[str, Any]]:
    """Every stage of one definition WITH its approver slots, stage_no ascending.

    Shaped so the result can be handed straight to :func:`build_stage`, which is
    what :func:`simulate` does -- a simulation must compile the stored
    configuration through exactly the code that routes a real object, or it
    simulates something else.
    """
    rows = session.fetchall(
        """
        SELECT stage_id, stage_no, name, parallel_group, quorum_type, quorum_n,
               applies_when, sla_hours, escalate_after_hours, escalate_to,
               allow_delegation, requires_reason, reason_code_set
        FROM approval_stage WHERE definition_id = %s ORDER BY stage_no
        """,
        (definition_id,))
    keys = ("stage_id", "stage_no", "name", "parallel_group", "quorum_type",
            "quorum_n", "applies_when", "sla_hours", "escalate_after_hours",
            "escalate_to", "allow_delegation", "requires_reason",
            "reason_code_set")
    stages = [dict(zip(keys, row)) for row in rows]
    if not stages:
        return []

    approvers = session.fetchall(
        "SELECT stage_id, ordinal, approver_kind, approver_ref, scope_expr "
        "FROM approval_stage_approver WHERE stage_id = ANY(%s) "
        "ORDER BY stage_id, ordinal",
        ([s["stage_id"] for s in stages],))
    by_stage: dict[str, list[dict[str, Any]]] = {}
    for stage_id, ordinal, kind, ref, scope_expr in approvers:
        by_stage.setdefault(stage_id, []).append(
            {"ordinal": ordinal, "approver_kind": kind, "approver_ref": ref,
             "scope_expr": scope_expr})
    for stage in stages:
        stage["approvers"] = by_stage.get(stage["stage_id"], [])
    return stages


def list_definitions(session, *, object_type: str | None = None,
                      status: str | None = None, cursor: str | None = None,
                      limit: int | None = None) -> dict[str, Any]:
    """``GET /api/approvals/definitions?object_type=&status=``.

    Scoped through ``repo.query``'s ``{scope}`` token (Contract 6). An
    organisation-wide definition -- ``entity_id IS NULL`` -- stays visible to
    every caller who may configure at all: it governs their entity too, and
    hiding it from an entity-restricted administrator would show them a
    workflow list that does not explain how their own objects route.

    The cursor is a keyset over ``(code, version)``, which
    ``uq_approval_definition_version`` makes unique, so a page boundary cannot
    repeat or drop a row when a new version is minted mid-walk -- which OFFSET
    pagination would.
    """
    from . import repo

    size = _page_size(limit)
    params: dict[str, Any] = {"lim": size + 1}
    clauses = ["(d.entity_id IS NULL OR {scope})"]
    if object_type:
        clauses.append("d.object_type = %(otype)s")
        params["otype"] = object_type
    if status:
        clauses.append("d.status = %(status)s")
        params["status"] = status
    if cursor:
        code, _, version = str(cursor).partition("\x1f")
        clauses.append("(d.code, d.version) > (%(c_code)s, %(c_version)s)")
        params["c_code"] = code
        try:
            params["c_version"] = int(version)
        except (TypeError, ValueError):
            params["c_version"] = 0

    rows = repo.query(
        session,
        f"""
        SELECT d.definition_id, d.object_type, d.code, d.version, d.status,
               d.entity_id, d.effective_from, d.effective_to, d.created_at,
               d.created_by, d.activated_at, d.activated_by, d.retired_at,
               d.retired_by
        FROM approval_definition d
        WHERE {' AND '.join(clauses)}
        ORDER BY d.code, d.version
        LIMIT %(lim)s
        """,
        params,
        columns=_DEFINITION_SCOPE_COLUMNS,
    )
    items = [_definition_row(row) for row in rows]
    if items:
        counts = session.fetchall(
            """
            SELECT d.definition_id,
                   (SELECT count(*) FROM approval_stage s
                     WHERE s.definition_id = d.definition_id),
                   (SELECT count(*) FROM approval_rule r
                     WHERE r.definition_id = d.definition_id)
            FROM approval_definition d WHERE d.definition_id = ANY(%s)
            """,
            ([i["definition_id"] for i in items],))
        counted = {row[0]: (int(row[1]), int(row[2])) for row in counts}
        for item in items:
            stages, rules = counted.get(item["definition_id"], (0, 0))
            item["stage_count"], item["rule_count"] = stages, rules

    return paginate(items, size, lambda row: f"{row['code']}\x1f{row['version']}")


def get_definition(session, definition_id: str) -> dict[str, Any]:
    """One definition row, or ``APPROVAL_DEFINITION_NOT_FOUND``."""
    row = session.fetchone(
        f"SELECT {_DEFINITION_COLUMNS} FROM approval_definition "
        f"WHERE definition_id = %s",
        (definition_id,))
    if row is None:
        raise ApprovalError("APPROVAL_DEFINITION_NOT_FOUND",
                             f"Approval definition {definition_id} does not exist.",
                             status=404)
    return _definition_row(row)


def list_versions(session, *, definition_id: str, cursor: str | None = None,
                   limit: int | None = None) -> dict[str, Any]:
    """``GET /api/approvals/definitions/{id}/versions``.

    Every version of the SAME ``(object_type, code)`` as ``definition_id``, not
    only the row the caller named: Contract 1 makes a change a new *version*,
    so "the versions of this workflow" is a question about the code, and
    answering it with the single row already in hand answers a different one.
    """
    anchor = get_definition(session, definition_id)
    size = _page_size(limit)
    params: dict[str, Any] = {"otype": anchor["object_type"],
                              "code": anchor["code"], "lim": size + 1}
    where = "object_type = %(otype)s AND code = %(code)s"
    if cursor:
        try:
            params["c_version"] = int(cursor)
        except (TypeError, ValueError):
            params["c_version"] = 0
        where += " AND version > %(c_version)s"
    rows = session.fetchall(
        f"SELECT {_DEFINITION_COLUMNS} FROM approval_definition "
        f"WHERE {where} ORDER BY version LIMIT %(lim)s",
        params)
    items = [_definition_row(row) for row in rows]
    return paginate(items, size, lambda row: str(row["version"]))


def _next_version(session, object_type: str, code: str) -> int:
    (highest,) = session.fetchone(
        "SELECT COALESCE(MAX(version), 0) FROM approval_definition "
        "WHERE object_type = %s AND code = %s",
        (object_type, code))
    return int(highest) + 1


def create_definition(session, *, actor_user_id: str,
                       payload: Mapping[str, Any]) -> dict[str, Any]:
    """``POST /api/approvals/definitions`` -- always a **DRAFT** version.

    The version is minted here and is never the caller's to choose (Contract
    1); the request model in ``api/approvals.py`` does not even carry the
    field. A code that already exists gets the next version number, which is
    what "change = a new version, the old row is retired" means in practice.

    Everything is COMPILED before anything is written. ``build_definition`` and
    ``build_stage`` are the same functions routing uses, so a predicate that
    would fail at 2am against a live document fails here instead, at the moment
    somebody typed it, and no half-written workflow is left behind.
    ``created_by`` is supplied on all three tables (NOT NULL, no default); none
    of them has an ``updated_by``, so none is written.

    No ``correlation_id``, deliberately
    -----------------------------------
    This function used to accept one and echo it back in the response dict
    without storing it anywhere. ``approval_definition``, ``approval_rule`` and
    ``approval_stage`` have no correlation column, this path writes no
    ``audit_log`` row, and migration 008 is lead-owned. A parameter that is
    accepted, carried the length of the function and then dropped reads at the
    call site as though the id were recorded -- which is worse than not
    offering it, because the next person to look for the trace believes one
    exists. The router still returns ``X-Correlation-Id`` on the response, and
    that is honestly the whole of the trace this path has.
    """
    object_type = (payload.get("object_type") or "").strip()
    code = (payload.get("code") or "").strip()
    if not object_type or not code:
        raise ApprovalError(
            "APPROVAL_DEFINITION_INVALID",
            "A definition needs both an object_type and a code.", status=400)

    rules_in = list(payload.get("rules") or [])
    stages_in = list(payload.get("stages") or [])

    # ---- shape, before compilation --------------------------------------
    # `build_definition` and `build_stage` subscript `priority`, `predicate`
    # and `stage_no` directly, which is right for the internal callers that
    # build them from a row they just SELECTed. This one builds them from a
    # request body, and a bare `KeyError` reaching `api/approvals.py` has no
    # `.code` -- `_raise_for_engine_error` re-raises it and the caller is told
    # their malformed rule was a server fault. Checked here rather than by
    # loosening `build_*`, so the compilers stay strict for the paths that
    # cannot get this wrong.
    for index, raw in enumerate(rules_in):
        missing = [key for key in ("priority", "predicate")
                   if not isinstance(raw, Mapping) or key not in raw]
        if missing:
            raise ApprovalError(
                "APPROVAL_DEFINITION_INVALID",
                f"rules[{index}] is missing {', '.join(missing)}. A rule needs "
                f"a priority (routing is 'first matching rule, priority ASC') "
                f"and a predicate (a rule that matches nothing routes nothing).",
                status=400)
    for index, raw in enumerate(stages_in):
        if not isinstance(raw, Mapping) or "stage_no" not in raw:
            raise ApprovalError(
                "APPROVAL_DEFINITION_INVALID",
                f"stages[{index}] is missing stage_no, which is what orders the "
                f"waves and what UNIQUE (definition_id, stage_no) is declared "
                f"on. There is no defensible default for it.",
                status=400)

    if not stages_in:
        raise ApprovalError(
            "APPROVAL_DEFINITION_INVALID",
            "A definition with no stages approves nothing and is not a licence "
            "to approve everything (Contract 2). Declare at least one stage.",
            status=400)

    version = _next_version(session, object_type, code)
    definition_id = _new_id("APD")
    effective_from = _as_date(payload.get("effective_from"),
                               label="effective_from") or date.today()
    effective_to = _as_date(payload.get("effective_to"), label="effective_to")

    # ---- compile first, write second ------------------------------------
    build_definition({
        "definition_id": definition_id, "object_type": object_type, "code": code,
        "version": version, "status": DEF_DRAFT,
        "entity_id": payload.get("entity_id"),
        "effective_from": effective_from, "effective_to": effective_to,
        "rules": rules_in,
    })
    compiled_stages = [build_stage(stage) for stage in stages_in]
    seen_stage_nos: set[int] = set()
    for stage in compiled_stages:
        if stage.stage_no in seen_stage_nos:
            raise ApprovalError(
                "APPROVAL_DEFINITION_INVALID",
                f"Two stages share stage_no {stage.stage_no}; UNIQUE "
                f"(definition_id, stage_no) forbids it and the wave order would "
                f"be undefined.", status=400)
        seen_stage_nos.add(stage.stage_no)
        if not stage.approvers:
            raise ApprovalError(
                "APPROVAL_DEFINITION_INVALID",
                f"Stage {stage.stage_no} ({stage.name}) names no approver. A "
                f"stage nobody is asked to act on cannot meet a quorum, and a "
                f"quorum that cannot be met is not a route to approval.",
                status=400)

    session.execute(
        """
        INSERT INTO approval_definition
            (definition_id, object_type, code, version, status, entity_id,
             effective_from, effective_to, created_by)
        VALUES (%(id)s, %(otype)s, %(code)s, %(version)s, %(status)s, %(entity)s,
                %(eff_from)s, %(eff_to)s, %(by)s)
        """,
        {"id": definition_id, "otype": object_type, "code": code,
         "version": version, "status": DEF_DRAFT,
         "entity": payload.get("entity_id"), "eff_from": effective_from,
         "eff_to": effective_to, "by": actor_user_id},
    )
    for raw in rules_in:
        session.execute(
            """
            INSERT INTO approval_rule
                (rule_id, definition_id, priority, predicate, description,
                 created_by)
            VALUES (%(id)s, %(def)s, %(priority)s, %(predicate)s, %(desc)s,
                    %(by)s)
            """,
            {"id": _new_id("APR"), "def": definition_id,
             "priority": int(raw["priority"]),
             "predicate": Jsonb(raw["predicate"]),
             "desc": raw.get("description"), "by": actor_user_id},
        )
    for index, raw in enumerate(stages_in):
        stage = compiled_stages[index]
        stage_id = _new_id("APS")
        session.execute(
            """
            INSERT INTO approval_stage
                (stage_id, definition_id, stage_no, name, parallel_group,
                 quorum_type, quorum_n, applies_when, sla_hours,
                 escalate_after_hours, escalate_to, allow_delegation,
                 requires_reason, reason_code_set, created_by)
            VALUES (%(id)s, %(def)s, %(no)s, %(name)s, %(group)s, %(qtype)s,
                    %(qn)s, %(applies)s, %(sla)s, %(esc_after)s, %(esc_to)s,
                    %(delegable)s, %(reason)s, %(reason_set)s, %(by)s)
            """,
            {"id": stage_id, "def": definition_id, "no": stage.stage_no,
             "name": stage.name, "group": stage.parallel_group,
             "qtype": stage.quorum_type, "qn": stage.quorum_n,
             "applies": (None if raw.get("applies_when") is None
                          else Jsonb(raw["applies_when"])),
             "sla": stage.sla_hours, "esc_after": stage.escalate_after_hours,
             "esc_to": (None if stage.escalate_to is None
                         else Jsonb(dict(stage.escalate_to))),
             "delegable": stage.allow_delegation,
             "reason": stage.requires_reason,
             "reason_set": stage.reason_code_set, "by": actor_user_id},
        )
        for ordinal, approver in enumerate(stage.approvers, start=1):
            session.execute(
                """
                INSERT INTO approval_stage_approver
                    (stage_id, ordinal, approver_kind, approver_ref, scope_expr)
                VALUES (%(stage)s, %(ordinal)s, %(kind)s, %(ref)s, %(scope)s)
                """,
                {"stage": stage_id,
                 "ordinal": int(approver.get("ordinal") or ordinal),
                 "kind": approver.get("approver_kind"),
                 "ref": approver.get("approver_ref"),
                 "scope": (None if approver.get("scope_expr") is None
                            else Jsonb(approver["scope_expr"]))},
            )

    return get_definition(session, definition_id)


def activate_definition(session, *, definition_id: str,
                         actor_user_id: str) -> dict[str, Any]:
    """``POST /api/approvals/definitions/{id}/activate`` -- DRAFT -> ACTIVE.

    Retiring the version this one supersedes happens in the SAME transaction,
    not as a follow-up call. Two ACTIVE versions of one ``(object_type, code)``
    would both be candidates at routing, and which one won would be decided by
    ``candidate_definitions``' tie-break rather than by anybody -- so leaving
    the predecessor ACTIVE is not a smaller change, it is a worse one.

    ``DEFINITION_IMMUTABLE`` for anything that is not a DRAFT: an ACTIVE version
    is already live and a RETIRED one is terminal. Migration 008's trigger says
    the same in SQLSTATE 42501; this is the readable refusal in front of it.
    """
    row = session.fetchone(
        "SELECT status, object_type, code, version, entity_id "
        "FROM approval_definition WHERE definition_id = %s FOR UPDATE",
        (definition_id,))
    if row is None:
        raise ApprovalError("APPROVAL_DEFINITION_NOT_FOUND",
                             f"Approval definition {definition_id} does not exist.",
                             status=404)
    status, object_type, code, _version, _entity_id = row
    if status != DEF_DRAFT:
        raise ApprovalError(
            ERR_DEFINITION_IMMUTABLE,
            f"Approval definition {definition_id} is {status}, not DRAFT. An "
            f"ACTIVE version is already live and a RETIRED one is terminal; a "
            f"change means a new version (Contract 1).",
            status=409, detail={"status": status})

    (stage_count,) = session.fetchone(
        "SELECT count(*) FROM approval_stage WHERE definition_id = %s",
        (definition_id,))
    if not stage_count:
        raise ApprovalError(
            "APPROVAL_DEFINITION_INVALID",
            f"Approval definition {definition_id} defines no stages. Activating "
            f"it would put a workflow live that approves nothing and holds "
            f"every object it routes in EXCEPTION_PENDING.",
            status=409)

    superseded = [
        r[0] for r in session.fetchall(
            "SELECT definition_id FROM approval_definition "
            "WHERE object_type = %s AND code = %s AND status = %s "
            "AND definition_id <> %s ORDER BY version",
            (object_type, code, DEF_ACTIVE, definition_id))
    ]
    for previous in superseded:
        session.execute(
            "UPDATE approval_definition SET status = %s, retired_at = now(), "
            "retired_by = %s WHERE definition_id = %s",
            (DEF_RETIRED, actor_user_id, previous))

    session.execute(
        "UPDATE approval_definition SET status = %s, activated_at = now(), "
        "activated_by = %s WHERE definition_id = %s",
        (DEF_ACTIVE, actor_user_id, definition_id))

    activated = get_definition(session, definition_id)
    activated["retired_definition_ids"] = superseded
    return activated


def simulate(session, *, definition_id: str, obj: Mapping[str, Any],
              actor_user_id: str | None = None,
              business_date: date | None = None) -> dict[str, Any]:
    """``POST /api/approvals/definitions/{id}/simulate`` -- a DRY RUN.

    Reads configuration and role membership, writes nothing at all, and answers
    the two questions an administrator has before activating a workflow: **would
    this object route here**, and **which stages, and whose names, would it put
    in front of the object**.

    It deliberately does NOT apply the contributor filter. Contract 5 removes
    the maker, the editors and the prior actors of a REAL document, and a
    simulated object has none of those -- a simulation that pretended otherwise
    would either invent a maker or report an approver population no actual
    submission will see. The route is answered as configured; who is barred from
    it is a property of the document, decided at ``open_instance`` and again at
    ``decide``. The payload says ``contributor_filter_applied: false`` so
    nobody reads the list as a promise about a particular document.
    """
    from . import approvals as approvals_mod

    definition = get_definition(session, definition_id)
    business_date = business_date or date.today()
    snapshot = dict(obj or {})
    entity_id = snapshot.get("entity_id") or definition["entity_id"]

    spec = build_definition({**definition,
                              "rules": load_definition_rules(session, definition_id)})

    matched_rule = None
    for rule in spec.rules:
        if rule.predicate.evaluate(snapshot):
            matched_rule = rule
            break

    eligible = bool(candidate_definitions(
        [spec], object_type=definition["object_type"],
        business_date=business_date, entity_id=entity_id))

    stages_out: list[dict[str, Any]] = []
    for raw in load_definition_stages(session, definition_id):
        stage = build_stage(raw)
        applies, skip_reason = stage_applies(stage, snapshot)
        entry: dict[str, Any] = {
            "stage_no": stage.stage_no, "name": stage.name,
            "parallel_group": stage.parallel_group,
            "quorum_type": stage.quorum_type, "quorum_n": stage.quorum_n,
            "applies": applies, "skip_reason": skip_reason,
            "requires_reason": stage.requires_reason,
            "sla_hours": stage.sla_hours,
            "assignees": [], "assignee_count": 0, "quorum_required": None,
        }
        if applies:
            # The engine's own resolver, not a reimplementation of it: a
            # simulation computed a different way simulates different code.
            resolved = approvals_mod.resolve_stage_assignees(
                session, stage, snapshot, ())
            entry["assignees"] = [
                {"user_id": a.user_id, "assigned_via": a.assigned_via,
                 "delegated_from": a.delegated_from} for a in resolved.assignees]
            entry["assignee_count"] = len(resolved)
            entry["quorum_required"] = required_quorum(
                stage.quorum_type, stage.quorum_n, len(resolved))
        stages_out.append(entry)

    return {
        "definition_id": definition_id,
        "object_type": definition["object_type"],
        "code": definition["code"],
        "version": definition["version"],
        "status": definition["status"],
        "eligible_on_date": eligible,
        "business_date": business_date.isoformat(),
        "would_route": bool(matched_rule) and eligible,
        "matched_rule_priority": matched_rule.priority if matched_rule else None,
        "matched_rule_id": matched_rule.rule_id if matched_rule else None,
        "route_unresolved_reason": (
            None if (matched_rule and eligible)
            else ("no rule of this definition matches the object" if eligible
                  else f"this definition is {definition['status']} or not in "
                       f"effect on {business_date.isoformat()} for entity "
                       f"{entity_id!r}")),
        "stages": stages_out,
        "contributor_filter_applied": False,
        "simulated_by": actor_user_id,
    }
