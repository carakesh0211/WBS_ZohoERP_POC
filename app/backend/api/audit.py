"""FastAPI router for the audit trail.

Exact contract this module is built to (the frontend agent codes against it in
parallel, so it must not drift)::

    GET /api/audit/streams
      -> {"items":[{"stream_key":str,"entry_count":int,"head_seq":int,"last_at":iso8601}]}
    GET /api/audit/entries?stream_key=&object_type=&object_id=&cursor=&limit=
      -> {"items":[{"audit_id":int,"stream_key":str,"seq":int,"at":iso8601,"actor":str,
                     "action":str,"object_type":str,"object_id":str,"detail":str,
                     "correlation_id":str|null,"entry_hash":str|null}],
          "next_cursor":str|null,"has_more":bool}
    GET /api/audit/chain/verify?stream_key=
      -> {"stream_key":str,"intact":bool,"entries_checked":int,
          "first_break_seq":int|null,"stream_found":bool,
          "sequence_contiguous":bool,"head_seq":int|null,
          "whole_stream_truncation_note":str,"verified_at":iso8601}
    GET /api/audit/anchors/verify
      -> {"anchored":bool,"intact":bool,"state":str,"anchors_checked":int,
          "anchor_chain_intact":bool,"first_broken_anchor_date":str|null,
          "malformed_anchor_dates":[str],
          "newest_anchor_date":str|null,"anchor_age_days":int|null,
          "anchor_stale":bool,"streams_anchored":int,
          "streams_anchored_ever":int,
          "missing_streams":[str],"truncated_streams":[obj],
          "diverged_streams":[obj],"dropped_from_newest_anchor":[obj],
          "note":str,"verified_at":iso8601}

`state` is the field to read, not `intact`. It is one of
``NEVER_ANCHORED`` | ``ANCHOR_INVALID_OR_STALE`` | ``MISSING_STREAM`` |
``TRUNCATED_AFTER_LAST_ANCHOR`` | ``DIVERGED_BELOW_ANCHOR`` |
``INTACT_ANCHORED`` (``pg.audit.ANCHOR_STATE_*``). ``intact: false`` collapses
five materially different situations into one, and the commonest of them --
nobody has ever run the anchor writer -- calls for a scheduler fix, not an
investigation. Existing keys are unchanged; the additions are additive.

`router = APIRouter()` is exported and mounted by `app/backend/main.py`, which
this module does not touch. Its routes carry their full `/api/audit/...` path
so mounting needs no prefix.

**Scope note.** `audit_log` carries no entity/plant/project/location columns
of its own -- it is filtered by `stream_key` / `object_type` / `object_id`,
not by the row-level scope dimensions `repo.query()` compiles. These routes
therefore open their database session with an unrestricted, service-level
`Scope` (see `_audit_service_scope`) rather than a per-request one; *who* may
read a given stream is a permissions concern for Milestone 4 to layer on top
of this router (e.g. a dependency that 403s before the handler runs), not
something this milestone's contract can express since no identity/auth
wiring exists yet.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from ..pg import principal_scope
from ..pg.audit import verify_anchors, verify_chain
from ..pg.engine import Database, Scope, get_database

class _AuditRead:
    """Router-level dependency enforcing `audit.read` on EVERY audit route.

    Declared on the ROUTER, not per route, so a future route cannot be added
    without a guard -- which is exactly how these three shipped unprotected:
    each declared only its database dependency, and the middleware in front of
    them checks that a session header EXISTS without validating it.

    The legacy SQLite `/api/audit/verify` has always required this permission.
    These routes register first and shadow it, so omitting the check did not
    merely leave new code open, it silently REMOVED an existing control.
    """

    def __call__(self, request: Request) -> dict:
        # Imported lazily: `main` imports this module, so a module-level
        # import would be circular.
        from ..main import principal
        from .. import auth as auth_mod

        who = principal(
            authorization=request.headers.get("Authorization", ""),
            x_session=request.headers.get("X-Session", ""),
        )
        try:
            auth_mod.require(who, "audit.read")
        except auth_mod.AuthError as exc:
            raise HTTPException(exc.status,
                                {"code": exc.code, "message": exc.message})
        request.state.audit_principal = who
        return who


require_audit_read = _AuditRead()

router = APIRouter(dependencies=[Depends(require_audit_read)])

_CORRELATION_HEADER = "X-Correlation-Id"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _audit_service_scope(request: Request | None = None,
                          database: Database | None = None) -> Scope:
    """The caller's REAL scope, resolved from their grants.

    This granted `read_all` to a set of role NAMES. Contract 2: `read_all`
    comes from `user_access_flag` and nowhere else. A role that genuinely
    needs the whole estate gets it by carrying the flag, which is auditable
    and revocable; a role name in a frozenset is neither.
    """
    who = {}
    if request is not None:
        who = getattr(request.state, "audit_principal", None) or {}
    if database is None:
        return principal_scope.denied_scope(
            principal_scope.principal_id(who) or "ANONYMOUS")
    return principal_scope.scope_for_request(database, who)




def _get_database() -> Database:
    """Degrade to a clean 503, never a 500 traceback.

    If this router is ever reached on a process with no PostgreSQL configured,
    that is a deployment fault, not a caller fault -- and the caller deserves a
    diagnosable status rather than an unhandled RuntimeError. Defence in depth
    behind the conditional mount in main.py.
    """
    try:
        return get_database()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_NOT_CONFIGURED",
                    "message": "The PostgreSQL audit API is mounted but no "
                               "database is configured for this process."},
        ) from exc


def _correlation_id(request: Request) -> str:
    return request.headers.get(_CORRELATION_HEADER) or str(uuid4())


def _set_correlation_header(response: Response, request: Request) -> None:
    response.headers[_CORRELATION_HEADER] = _correlation_id(request)


def _problem(status_code: int, code: str, title: str,
             detail: str | None = None) -> HTTPException:
    """An RFC-7807-shaped error body, carrying the `code` field the frontend
    contract requires. Raised as `HTTPException.detail`: without a global
    exception handler installed on the app instance (owned by `main.py`, which
    this router does not touch), FastAPI serialises `detail` verbatim as the
    JSON response body, so the shape below is what a client actually receives.
    """
    return HTTPException(status_code=status_code, detail={
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "code": code,
        "detail": detail,
    })


def _encode_cursor(audit_id: int) -> str:
    return base64.urlsafe_b64encode(str(audit_id).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        return int(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise _problem(400, "INVALID_CURSOR",
                       "Invalid pagination cursor",
                       f"cursor {cursor!r} could not be decoded") from exc


def _row_to_entry(row: tuple) -> dict[str, Any]:
    (audit_id, stream_key, seq, at, actor, action, object_type, object_id,
     detail, correlation_id, entry_hash) = row
    return {
        "audit_id": audit_id,
        "stream_key": stream_key,
        "seq": seq,
        "at": at.isoformat() if hasattr(at, "isoformat") else at,
        "actor": actor,
        "action": action,
        "object_type": object_type,
        "object_id": object_id,
        "detail": detail,
        "correlation_id": correlation_id,
        "entry_hash": entry_hash,
    }


@router.get("/api/audit/streams")
def list_streams(response: Response, request: Request,
                  database: Database = Depends(_get_database)) -> dict[str, Any]:
    _set_correlation_header(response, request)
    with database.session(_audit_service_scope()) as session:
        rows = session.fetchall(
            """
            SELECT stream_key, COUNT(*) AS entry_count, MAX(seq) AS head_seq,
                   MAX(at) AS last_at
            FROM audit_log
            GROUP BY stream_key
            ORDER BY stream_key
            """
        )
    items = [
        {
            "stream_key": stream_key,
            "entry_count": entry_count,
            "head_seq": head_seq,
            "last_at": last_at.isoformat() if hasattr(last_at, "isoformat") else last_at,
        }
        for stream_key, entry_count, head_seq, last_at in rows
    ]
    return {"items": items}


@router.get("/api/audit/entries")
def list_entries(
    response: Response,
    request: Request,
    stream_key: str | None = Query(default=None),
    object_type: str | None = Query(default=None),
    object_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    limit = min(max(limit, 1), _MAX_LIMIT)
    before_id = _decode_cursor(cursor) if cursor else None

    conditions: list[str] = []
    params: dict[str, Any] = {}
    if stream_key is not None:
        conditions.append("stream_key = %(stream_key)s")
        params["stream_key"] = stream_key
    if object_type is not None:
        conditions.append("object_type = %(object_type)s")
        params["object_type"] = object_type
    if object_id is not None:
        conditions.append("object_id = %(object_id)s")
        params["object_id"] = object_id
    if before_id is not None:
        conditions.append("audit_id < %(before_id)s")
        params["before_id"] = before_id
    where = " AND ".join(conditions) if conditions else "TRUE"
    # Fetch one extra row so `has_more` can be decided without a second query.
    params["fetch_limit"] = limit + 1

    with database.session(_audit_service_scope()) as session:
        rows = session.fetchall(
            f"""
            SELECT audit_id, stream_key, seq, at, actor, action, object_type,
                   object_id, detail, correlation_id, entry_hash
            FROM audit_log
            WHERE {where}
            ORDER BY audit_id DESC
            LIMIT %(fetch_limit)s
            """,
            params,
        )

    has_more = len(rows) > limit
    page = rows[:limit]
    items = [_row_to_entry(row) for row in page]
    next_cursor = _encode_cursor(page[-1][0]) if has_more and page else None
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@router.get("/api/audit/chain/verify")
def verify(
    response: Response,
    request: Request,
    stream_key: str = Query(...),
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    _set_correlation_header(response, request)
    if not stream_key:
        raise _problem(400, "STREAM_KEY_REQUIRED",
                       "stream_key is required", "stream_key must not be empty")

    with database.session(_audit_service_scope()) as session:
        result = verify_chain(session, stream_key)

    # Forward the evidence fields, not just the verdict.
    #
    # This handler originally rebuilt a fixed five-key response, so when
    # verify_chain gained stream_found, sequence_contiguous and head_seq --
    # the fields that distinguish "verified intact" from "verified nothing" --
    # they stopped at the service boundary and never reached a caller.
    #
    # `intact` alone is the field most likely to be trusted and least able to
    # justify itself: an unknown stream_key used to report intact=True with
    # entries_checked=0. A caller has to be able to see WHY.
    return {
        "stream_key": stream_key,
        "intact": result["intact"],
        "entries_checked": result["entries_checked"],
        "first_break_seq": result["first_break_seq"],
        "stream_found": result["stream_found"],
        "sequence_contiguous": result["sequence_contiguous"],
        "head_seq": result["head_seq"],
        "whole_stream_truncation_note": result["whole_stream_truncation_note"],
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/audit/anchors/verify")
def verify_anchors_route(
    response: Response,
    request: Request,
    database: Database = Depends(_get_database),
) -> dict[str, Any]:
    """The question `/api/audit/chain/verify` provably cannot answer.

    A per-stream chain proves that the rows a stream STILL HAS link to each
    other. It cannot see rows removed from the end, and it cannot see a stream
    that no longer exists -- both leave a self-consistent database, and the
    second one leaves nothing to ask a question about. Only the daily anchors
    remember what was there.

    READ-ONLY, and deliberately. This route verifies; it does not write an
    anchor. Writing one is a scheduled job's work, and giving it an HTTP verb
    would mean deciding which role may cause an anchor to exist -- a
    role-mapping decision recorded against D-12, not one to make in passing
    while adding a verifier. The router's `audit.read` guard applies here as
    it does to every other route in this file, so an Auditor -- the role whose
    whole purpose is this question -- can ask it without being granted a
    single new permission.

    `anchored: false` is NOT a pass. It means nobody has ever written an
    anchor, which is precisely the state in which a deleted stream is
    undetectable, and reporting that as intact is the defect the anchor exists
    to close.
    """
    _set_correlation_header(response, request)

    with database.session(_audit_service_scope()) as session:
        result = verify_anchors(session)

    return {**result, "verified_at": datetime.now(timezone.utc).isoformat()}
