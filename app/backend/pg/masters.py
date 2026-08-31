"""Item and vendor masters, custom fields, and numbering -- the service layer.

See ``docs/WAVE2_CONTRACTS.md`` ("API contract -- Settings and masters") for
the shape every function here ultimately serves, and
``.claude/skills/wbs-full-app-builder/references/zoho-boundaries.md`` for the
honesty rules a ``source='ZOHO'`` row is held to.

Five invariants this module exists to make mechanical rather than remembered:

1. **A `ZOHO`-sourced row is never created through this API, and its business
   fields are never locally edited.** :func:`create_local` unconditionally
   forces ``source='LOCAL'`` regardless of what a caller's payload contains;
   :func:`ingest_from_adapter` is the only way a ``ZOHO`` row is written, and
   it is the adapter seam, not a request handler. :func:`update_master`
   refuses a payload that changes a ZOHO row's business fields, with a clear
   ``ZOHO_SOURCED_FIELD_READONLY`` code, rather than silently accepting or
   silently ignoring the edit.
2. **Nothing here is ever marked `LIVE` or `VERIFIED`.** There is no live
   Zoho connection in this wave; :func:`ingest_from_adapter` refuses any
   ``source_of_truth_status`` outside ``{MOCK, UNVERIFIED}``. A false
   `VERIFIED` is a lie the whole product's evidentiary posture rests on.
3. **`gst_no` / `pan_no` are never hashed, anywhere.** They exist to be
   matched against statutory filings and Zoho records; hashing destroys that.
   They are protected by masking (:func:`mask_gst_no`, :func:`mask_pan_no`,
   funnelled through the single formatter :func:`render_vendor` so no
   endpoint can forget to call it) and by a reveal permission that is
   checked and audited by the caller (``app/backend/api/masters.py``).
4. **Optimistic concurrency is provable, not advisory.** :func:`update_master`
   takes the row lock, compares `version_no` under that lock, and returns a
   409 *before* issuing any UPDATE on a mismatch -- the row is providably
   unchanged, not just usually unchanged.
5. **Numbering cannot mint the same value twice under concurrency.** See
   :func:`issue_number`'s docstring for why this is an atomic
   ``INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING``, not the
   `SELECT COUNT(*) + 1` pattern this project has already shipped once and
   is not allowed to ship again.

Every mutation ends with :func:`app.backend.pg.audit.append`. Deactivation
never deletes a row -- financial and master data are not hard-deleted in this
product.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from uuid import uuid4

from psycopg.types.json import Jsonb

from . import audit as pg_audit
from .engine import Session

# ============================================================================
# Errors
# ============================================================================
class MasterDataError(Exception):
    """Carries the HTTP status and machine-readable `code` the API layer
    needs to build an RFC-7807 body. Never raised for "not found under this
    caller's scope" -- see repo.py's module docstring for why that
    distinction matters; this module has no row-level scope concept of its
    own (settings/masters are organisation-wide reference data in this
    milestone -- see api/settings.py and api/masters.py module docstrings)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


NOT_FOUND = "NOT_FOUND"
VERSION_CONFLICT = "VERSION_CONFLICT"
MISSING_FIELD = "MISSING_FIELD"
UNKNOWN_FIELD = "UNKNOWN_FIELD"
MASKED_VALUE_SUBMITTED = "MASKED_VALUE_SUBMITTED"
ZOHO_CREATE_REFUSED = "ZOHO_SOURCE_NOT_CREATABLE"
ZOHO_FIELD_READONLY = "ZOHO_SOURCED_FIELD_READONLY"
INVALID_SOURCE_OF_TRUTH = "SOURCE_OF_TRUTH_NOT_ALLOWED"
NUMBERING_SERIES_NOT_FOUND = "NUMBERING_SERIES_NOT_FOUND"

VALID_SOURCES = ("LOCAL", "IMPORT", "ZOHO")

#: `source_of_truth_status` values an ingestion (adapter/fixture) path may
#: assign. `LIVE` and `VERIFIED` are valid column VALUES (a later milestone
#: with a real, authorised Zoho connection will use them) but this wave has
#: no such connection, so nothing written through :func:`ingest_from_adapter`
#: may claim either -- see the module docstring, invariant 2.
ALLOWED_INGEST_TRUTH_STATUS = ("MOCK", "UNVERIFIED")


# ============================================================================
# Kinds -- item vs vendor, the only two differences the rest of this module
# needs to know about.
# ============================================================================
@dataclass(frozen=True)
class MasterKind:
    table: str
    id_column: str
    id_prefix: str
    numbering_series_code: str
    #: Every caller-settable business field, in the order a fresh row is
    #: built. `code` and `name` are always first and always required.
    business_fields: tuple[str, ...]


ITEM = MasterKind(
    table="item_master",
    id_column="item_id",
    id_prefix="ITM",
    numbering_series_code="ITEM",
    business_fields=("code", "name", "uom", "category", "hsn_code", "description"),
)

VENDOR = MasterKind(
    table="vendor_master",
    id_column="vendor_id",
    id_prefix="VEN",
    numbering_series_code="VENDOR",
    business_fields=(
        "code", "name", "gst_no", "gst_treatment", "place_of_contact",
        "pan_no", "description",
    ),
)

KINDS: dict[str, MasterKind] = {"items": ITEM, "vendors": VENDOR}

#: Fields a caller may change regardless of `source` -- local governance
#: metadata, not the mirrored business content. Distinct from
#: `MasterKind.business_fields`, which is exactly what invariant 1 protects
#: on a ZOHO row.
GOVERNANCE_FIELDS = ("mapping_status", "duplicate_of")

def kind_for(name: str) -> MasterKind:
    kind = KINDS.get(name)
    if kind is None:
        raise MasterDataError(404, "UNKNOWN_MASTER_KIND",
                               f"no master data kind {name!r}; expected one of "
                               f"{sorted(KINDS)}")
    return kind


def _new_id(kind: MasterKind) -> str:
    return f"{kind.id_prefix}-{uuid4().hex[:12].upper()}"


def row_columns(kind: MasterKind) -> tuple[str, ...]:
    return (kind.id_column,) + kind.business_fields + (
        "normalised_code", "normalised_name",
        "source", "external_source", "external_id", "external_last_modified",
        "payload_sha", "source_of_truth_status", "duplicate_of", "mapping_status",
        "is_active", "created_at", "created_by", "updated_at", "updated_by",
        "version_no",
    )


def row_to_dict(kind: MasterKind, row: tuple) -> dict[str, Any]:
    columns = row_columns(kind)
    out: dict[str, Any] = {}
    for column, value in zip(columns, row):
        out[column] = value.isoformat() if hasattr(value, "isoformat") else value
    return out


def _select_row(session: Session, kind: MasterKind, id_value: str,
                 *, for_update: bool = False) -> tuple | None:
    columns = ", ".join(row_columns(kind))
    suffix = " FOR UPDATE" if for_update else ""
    return session.fetchone(
        f"SELECT {columns} FROM {kind.table} WHERE {kind.id_column} = %s{suffix}",  # noqa: S608 -- table/columns from a fixed internal allow-list, never user input
        (id_value,))


# ============================================================================
# Normalisation and duplicate detection -- pure Python mirror of
# `capex_normalise_text()` in 005_master_data.sql, so duplicate logic is unit
# testable without a live database. Keep the two in step.
# ============================================================================
_NORMALISE_RE = re.compile(r"[^a-zA-Z0-9]+")


def normalise_text(value: str) -> str:
    """Mirrors the SQL `capex_normalise_text()` generated-column expression
    exactly: lower-case, every run of non-alphanumeric characters removed."""
    return _NORMALISE_RE.sub("", value).lower()


def find_duplicate(session: Session, kind: MasterKind, *, exclude_id: str | None,
                    code: str, name: str) -> tuple[str, str] | None:
    """The first other row sharing a normalised code or name, if any.

    Returns `(id, reason)` where `reason` is `"code"` or `"name"` -- never
    both merged into one, since a caller (or a test) may want to know which
    matched. Only ever FLAGS a candidate; never merges, deletes or rejects
    the caller's row. See the module docstring's precedent in
    `docs/WAVE2_CONTRACTS.md`: "flag, never auto-merge."
    """
    norm_code, norm_name = normalise_text(code), normalise_text(name)
    exclude = exclude_id or ""
    row = session.fetchone(
        f"""
        SELECT {kind.id_column},
               CASE WHEN normalised_code = %(norm_code)s THEN 'code' ELSE 'name' END
        FROM {kind.table}
        WHERE {kind.id_column} <> %(exclude)s
          AND (normalised_code = %(norm_code)s OR normalised_name = %(norm_name)s)
        ORDER BY (normalised_code = %(norm_code)s) DESC, {kind.id_column}
        LIMIT 1
        """,  # noqa: S608 -- table/column from a fixed internal allow-list
        {"norm_code": norm_code, "norm_name": norm_name, "exclude": exclude},
    )
    return (row[0], row[1]) if row is not None else None


# ============================================================================
# Masking -- the ONE shared formatter. See the module docstring, invariant 3.
# ============================================================================
def _mask(value: str, head: int, tail: int) -> str:
    if len(value) <= head + tail:
        # Too short to safely reveal both ends without exposing the whole
        # value; fall back to revealing only the first character.
        return value[:1] + "*" * max(len(value) - 1, 0)
    return value[:head] + "****" + value[-tail:]


#: Fields rendered masked in any response, and therefore never accepted
#: back in a payload while they still carry the mask character.
MASKED_FIELDS = frozenset({"gst_no", "pan_no"})


def mask_gst_no(value: str | None) -> str | None:
    """`27ABCDE1234A1Z5` -> `27ABCDE****1Z5` -- first 7, last 3, fixed
    4-asterisk gap, matching the contract's worked example exactly."""
    return None if not value else _mask(value, 7, 3)


def mask_pan_no(value: str | None) -> str | None:
    """`ABCDE1234F` -> `ABCDE****F` -- first 5, last 1."""
    return None if not value else _mask(value, 5, 1)


#: `gst_no` and `pan_no` MUST NEVER appear in any hashlib call, anywhere in
#: this module. Regression-tested directly in tests/test_pg_masters.py by
#: scanning this file's own source -- a mistake this project has already
#: made once (entity.gst_no / entity.pan_no's own header note exists because
#: of it) and must not repeat.
def render_vendor(row: Mapping[str, Any], *, reveal: bool) -> dict[str, Any]:
    """The single formatter every endpoint MUST call to shape a vendor row
    for a caller. `gst_no`/`pan_no` are masked unless `reveal` is True.

    Deliberately takes and returns a plain dict rather than mutating `row`,
    so a caller cannot accidentally reuse an unmasked dict after asking for
    a masked one.
    """
    out = dict(row)
    if not reveal:
        out["gst_no"] = mask_gst_no(out.get("gst_no"))
        out["pan_no"] = mask_pan_no(out.get("pan_no"))
    out["tax_identity_revealed"] = bool(reveal)
    return out


def render_item(row: Mapping[str, Any]) -> dict[str, Any]:
    """Items carry no regulated tax identity; this exists so callers have one
    uniform `render_*` entry point per kind rather than reaching into the raw
    row for items but not vendors."""
    return dict(row)


# ============================================================================
# Create
# ============================================================================
def create_local(session: Session, kind: MasterKind, *, actor: str,
                  payload: Mapping[str, Any],
                  correlation_id: str | None = None) -> dict[str, Any]:
    """Create a `source='LOCAL'` row.

    A ZOHO row is never created through this path. `source` is set here, not
    taken from `payload`, and a payload that tries to carry `source` at all is
    REFUSED as an unknown field rather than silently ignored -- a caller who
    believes they set a field they did not is worse off than one who is told.

    (The docstring previously said `source` was "forced regardless of what
    payload contains", which read as "ignored". The unknown-field check below
    runs first and refuses it. Code and docstring now agree, and the test
    asserts the refusal.)
    """
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    if not code:
        raise MasterDataError(422, MISSING_FIELD, "code is required")
    if not name:
        raise MasterDataError(422, MISSING_FIELD, "name is required")

    unknown = set(payload) - set(kind.business_fields)
    if unknown:
        raise MasterDataError(422, UNKNOWN_FIELD,
                               f"unknown field(s) for this master kind: {sorted(unknown)}")

    row_id = _new_id(kind)
    fields = {field: payload.get(field) for field in kind.business_fields}
    fields["code"], fields["name"] = code, name

    columns = [kind.id_column] + list(kind.business_fields) + [
        "source", "source_of_truth_status", "mapping_status", "is_active",
        "created_by", "updated_by",
    ]
    placeholders = ", ".join(f"%({c})s" for c in columns)
    params: dict[str, Any] = dict(fields)
    params[kind.id_column] = row_id
    params["source"] = "LOCAL"
    params["source_of_truth_status"] = "LOCAL"
    params["mapping_status"] = "UNMAPPED"
    params["is_active"] = True
    params["created_by"] = actor
    params["updated_by"] = actor

    session.execute(
        f"INSERT INTO {kind.table} ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
        params,
    )

    duplicate = find_duplicate(session, kind, exclude_id=row_id, code=code, name=name)
    if duplicate is not None:
        dup_id, _reason = duplicate
        session.execute(
            f"UPDATE {kind.table} SET duplicate_of = %s, mapping_status = 'DUPLICATE_SUSPECT' "  # noqa: S608
            f"WHERE {kind.id_column} = %s",
            (dup_id, row_id),
        )

    pg_audit.append(session, actor, "CREATE", kind.table, row_id,
                     f"code={code!r} name={name!r} source=LOCAL",
                     correlation_id=correlation_id)

    row = _select_row(session, kind, row_id)
    assert row is not None
    return row_to_dict(kind, row)


def ingest_from_adapter(session: Session, kind: MasterKind, *, actor: str,
                         external_source: str, external_id: str,
                         payload: Mapping[str, Any], payload_sha: str,
                         source_of_truth_status: str = "MOCK",
                         external_last_modified: Any = None,
                         correlation_id: str | None = None) -> dict[str, Any]:
    """The ONLY path that may write `source='ZOHO'`. Called by the adapter
    seam / fixtures (see `zoho-boundaries.md`), never by a request handler.

    Upserts on `(external_source, external_id)`: a re-sync of a row already
    seen updates it in place rather than creating a duplicate mirror. Refuses
    any `source_of_truth_status` outside :data:`ALLOWED_INGEST_TRUTH_STATUS`
    -- see the module docstring, invariant 2.
    """
    if source_of_truth_status not in ALLOWED_INGEST_TRUTH_STATUS:
        raise MasterDataError(
            422, INVALID_SOURCE_OF_TRUTH,
            f"source_of_truth_status {source_of_truth_status!r} is not permitted for an "
            f"ingested row in this wave (no live Zoho connection exists to have verified "
            f"anything against); allowed: {ALLOWED_INGEST_TRUTH_STATUS}")

    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    if not code or not name:
        raise MasterDataError(422, MISSING_FIELD, "code and name are required")

    existing = session.fetchone(
        f"SELECT {kind.id_column} FROM {kind.table} "  # noqa: S608
        f"WHERE external_source = %s AND external_id = %s",
        (external_source, external_id))

    row_id = existing[0] if existing is not None else _new_id(kind)
    fields = {field: payload.get(field) for field in kind.business_fields}
    fields["code"], fields["name"] = code, name

    if existing is None:
        columns = [kind.id_column] + list(kind.business_fields) + [
            "source", "external_source", "external_id", "external_last_modified",
            "payload_sha", "source_of_truth_status", "mapping_status", "is_active",
            "created_by", "updated_by",
        ]
        placeholders = ", ".join(f"%({c})s" for c in columns)
        params: dict[str, Any] = dict(fields)
        params.update({
            kind.id_column: row_id, "source": "ZOHO",
            "external_source": external_source, "external_id": external_id,
            "external_last_modified": external_last_modified,
            "payload_sha": payload_sha,
            "source_of_truth_status": source_of_truth_status,
            "mapping_status": "UNMAPPED", "is_active": True,
            "created_by": actor, "updated_by": actor,
        })
        session.execute(
            f"INSERT INTO {kind.table} ({', '.join(columns)}) VALUES ({placeholders})",  # noqa: S608
            params)
        action = "INGEST_CREATE"
    else:
        set_clause = ", ".join(f"{field} = %({field})s" for field in kind.business_fields)
        params = dict(fields)
        params.update({
            kind.id_column: row_id,
            "external_last_modified": external_last_modified,
            "payload_sha": payload_sha,
            "source_of_truth_status": source_of_truth_status,
            "updated_by": actor,
        })
        session.execute(
            f"""
            UPDATE {kind.table}
            SET {set_clause}, external_last_modified = %(external_last_modified)s,
                payload_sha = %(payload_sha)s,
                source_of_truth_status = %(source_of_truth_status)s,
                updated_by = %(updated_by)s, updated_at = now(),
                version_no = version_no + 1
            WHERE {kind.id_column} = %({kind.id_column})s
            """,  # noqa: S608
            params)
        action = "INGEST_UPDATE"

    duplicate = find_duplicate(session, kind, exclude_id=row_id, code=code, name=name)
    if duplicate is not None:
        dup_id, _reason = duplicate
        session.execute(
            f"UPDATE {kind.table} SET duplicate_of = %s, mapping_status = 'DUPLICATE_SUSPECT' "  # noqa: S608
            f"WHERE {kind.id_column} = %s",
            (dup_id, row_id))

    pg_audit.append(session, actor, action, kind.table, row_id,
                     f"external_source={external_source!r} external_id={external_id!r} "
                     f"source_of_truth_status={source_of_truth_status!r}",
                     correlation_id=correlation_id)

    row = _select_row(session, kind, row_id)
    assert row is not None
    return row_to_dict(kind, row)


# ============================================================================
# Update -- optimistic concurrency + the ZOHO field-edit refusal
# ============================================================================
def update_master(session: Session, kind: MasterKind, *, actor: str, id_value: str,
                   expected_version: int, payload: Mapping[str, Any],
                   correlation_id: str | None = None) -> dict[str, Any]:
    """Update a row, honouring `version_no` optimistic concurrency.

    Takes the row lock (`FOR UPDATE`) before comparing `version_no`, so the
    409 on a stale version is provable: nothing about this row changes
    between the lock and the raise, and no UPDATE is ever issued once a
    mismatch is found -- see the module docstring, invariant 4.

    A ZOHO-sourced row refuses any payload that would change a business
    field's value (invariant 1); governance fields
    (:data:`GOVERNANCE_FIELDS`) may always be changed regardless of source,
    since they are this product's own local classification of the row, not
    mirrored content.
    """
    unknown = set(payload) - set(kind.business_fields) - set(GOVERNANCE_FIELDS)
    if unknown:
        raise MasterDataError(422, UNKNOWN_FIELD,
                               f"unknown field(s) for this master kind: {sorted(unknown)}")

    # A masked value must never be written back as if it were the real one.
    #
    # A list response returns `gst_no` as `27ABCDE****1Z5`. An edit form that
    # loads a row, shows that value and submits it unchanged would write the
    # mask over the true tax identity -- and here it does not even fail
    # safely: `vendor_master` CHECKs `gst_no ~ '^[0-9]{2}[A-Z0-9]{13}$'`, so
    # `*` violates the constraint and psycopg raises CheckViolation, which the
    # router does not catch, so the caller gets a 500 and cannot edit the
    # vendor at all. Refusing here turns silent corruption -- and an
    # unhandled 500 -- into an actionable 422 that names the field.
    masked = sorted(field for field in MASKED_FIELDS & set(payload)
                    if isinstance(payload.get(field), str) and "*" in payload[field])
    if masked:
        raise MasterDataError(
            422, MASKED_VALUE_SUBMITTED,
            f"masked value(s) submitted for {masked}: a masked field cannot be "
            f"written back. Omit the field to leave it unchanged, or send the "
            f"full value.")

    current_row = _select_row(session, kind, id_value, for_update=True)
    if current_row is None:
        raise MasterDataError(404, NOT_FOUND, f"{kind.table} {id_value} does not exist")
    current = row_to_dict(kind, current_row)

    if current["version_no"] != expected_version:
        raise MasterDataError(
            409, VERSION_CONFLICT,
            f"{id_value} was modified by someone else (have version "
            f"{current['version_no']}, expected {expected_version}). Reload and try again.")

    business_changes = {
        field: payload[field] for field in kind.business_fields
        if field in payload and payload[field] != current.get(field)
    }
    if current["source"] == "ZOHO" and business_changes:
        raise MasterDataError(
            409, ZOHO_FIELD_READONLY,
            f"{id_value} is sourced from ZOHO; local edits to its business field(s) "
            f"{sorted(business_changes)} are refused. Only governance fields "
            f"({', '.join(GOVERNANCE_FIELDS)}) may be changed on a ZOHO-sourced row.")

    set_parts: list[str] = []
    params: dict[str, Any] = {"id_value": id_value, "expected_version": expected_version}
    for field in kind.business_fields:
        if field in payload:
            set_parts.append(f"{field} = %({field})s")
            params[field] = payload[field]
    for field in GOVERNANCE_FIELDS:
        if field in payload:
            set_parts.append(f"{field} = %({field})s")
            params[field] = payload[field]

    if not set_parts:
        # Nothing to change -- still a valid call (e.g. a client resubmitting
        # the same version to confirm no drift), but no UPDATE is needed.
        return current

    set_parts += ["updated_by = %(updated_by)s", "updated_at = now()",
                  "version_no = version_no + 1"]
    params["updated_by"] = actor

    session.execute(
        f"""
        UPDATE {kind.table} SET {', '.join(set_parts)}
        WHERE {kind.id_column} = %(id_value)s AND version_no = %(expected_version)s
        """,  # noqa: S608
        params,
    )

    if "code" in business_changes or "name" in business_changes:
        new_code = business_changes.get("code", current["code"])
        new_name = business_changes.get("name", current["name"])
        duplicate = find_duplicate(session, kind, exclude_id=id_value,
                                    code=new_code, name=new_name)
        if duplicate is not None:
            dup_id, _reason = duplicate
            session.execute(
                f"UPDATE {kind.table} SET duplicate_of = %s, mapping_status = 'DUPLICATE_SUSPECT' "  # noqa: S608
                f"WHERE {kind.id_column} = %s",
                (dup_id, id_value))

    pg_audit.append(session, actor, "UPDATE", kind.table, id_value,
                     f"fields={sorted(set(payload) & (set(kind.business_fields) | set(GOVERNANCE_FIELDS)))}",
                     correlation_id=correlation_id)

    row = _select_row(session, kind, id_value)
    assert row is not None
    return row_to_dict(kind, row)


def deactivate_master(session: Session, kind: MasterKind, *, actor: str, id_value: str,
                       correlation_id: str | None = None) -> dict[str, Any]:
    """Deactivate, never delete -- master data is not hard-deleted."""
    current_row = _select_row(session, kind, id_value, for_update=True)
    if current_row is None:
        raise MasterDataError(404, NOT_FOUND, f"{kind.table} {id_value} does not exist")

    session.execute(
        f"""
        UPDATE {kind.table}
        SET is_active = false, updated_by = %(actor)s, updated_at = now(),
            version_no = version_no + 1
        WHERE {kind.id_column} = %(id_value)s
        """,  # noqa: S608
        {"actor": actor, "id_value": id_value},
    )
    pg_audit.append(session, actor, "DEACTIVATE", kind.table, id_value, "",
                     correlation_id=correlation_id)
    row = _select_row(session, kind, id_value)
    assert row is not None
    return row_to_dict(kind, row)


def reveal_vendor_tax_identity(session: Session, vendor_id: str, *, actor: str,
                                reason: str, correlation_id: str | None = None
                                ) -> dict[str, Any]:
    """Fetch a vendor row unmasked, writing one audit entry per revealed
    field naming the actor, the field, and the reason -- the contract's
    exact requirement. The PERMISSION check itself is the API layer's job
    (`app/backend/api/masters.py`); this function is only reachable once
    that check has already passed, and it always audits when called."""
    row = _select_row(session, VENDOR, vendor_id)
    if row is None:
        raise MasterDataError(404, NOT_FOUND, f"vendor_master {vendor_id} does not exist")
    data = row_to_dict(VENDOR, row)

    for field in ("gst_no", "pan_no"):
        if data.get(field):
            pg_audit.append(
                session, actor, "REVEAL_TAX_IDENTITY", VENDOR.table, vendor_id,
                f"field={field} reason={reason!r}",
                correlation_id=correlation_id)

    return render_vendor(data, reveal=True)


def reveal_entity_tax_identity(session: Session, entity_id: str, *, actor: str,
                                reason: str, correlation_id: str | None = None
                                ) -> dict[str, Any]:
    """`reveal_vendor_tax_identity`'s counterpart for `entity`.

    `api/settings.py` called `_reveal_entity_tax_identity` -- a name that
    existed nowhere -- so every successful entity reveal raised NameError
    inside the session, rolled back, and returned 500. No entity reveal ever
    succeeded, and the module's central claim, that a full reveal always
    writes an audit entry, was never once exercised on this path.

    `entity` is not a MasterKind, so this reads the two regulated columns
    directly rather than through `_select_row`. As with the vendor form, the
    PERMISSION check belongs to the API layer; this function is only reachable
    once that check has passed, and it always audits when called.
    """
    row = session.fetchone(  # scope-exempt: settings data is organisation-wide reference data; the permission check is the control
        "SELECT gst_no, pan_no FROM entity WHERE entity_id = %s", (entity_id,))
    if row is None:
        raise MasterDataError(404, NOT_FOUND, f"entity {entity_id} does not exist")

    gst_no, pan_no = row
    for field, value in (("gst_no", gst_no), ("pan_no", pan_no)):
        if value:
            pg_audit.append(
                session, actor, "REVEAL_TAX_IDENTITY", "entity", entity_id,
                f"field={field} reason={reason!r}",
                correlation_id=correlation_id)

    return {"gst_no": gst_no, "pan_no": pan_no}


# ============================================================================
# Numbering
# ============================================================================
def issue_number(session: Session, series_code: str, *, actor: str,
                  period_key: str = "", object_type: str | None = None,
                  object_id: str | None = None) -> dict[str, Any]:
    """Atomically mint the next value for `series_code`/`period_key`.

    `INSERT ... ON CONFLICT (series_id, period_key) DO UPDATE ... RETURNING`
    is the whole safety property: PostgreSQL resolves the conflict by taking
    the row's lock before deciding whether to insert or update, so two
    concurrent callers for the same series/period are serialised by the
    database itself, not by application discipline. This is the exact
    replacement for the `SELECT COUNT(*) + 1` pattern the module docstring
    warns against -- that pattern reads under no lock at all, so two
    concurrent readers can observe the same count and both mint the same
    next number.

    The value is then written to the append-only `numbering_issued` table in
    the SAME transaction, so a caller that rolls back after this call never
    leaves a counter advanced with no corresponding issued record -- but note
    the value itself is still consumed (not reused) even on rollback, by
    design: a gap in the sequence is acceptable, a reused number is not.
    """
    series = session.fetchone(
        "SELECT series_id, prefix, suffix, pad_width FROM numbering_series "
        "WHERE code = %s AND is_active",
        (series_code,))
    if series is None:
        raise MasterDataError(404, NUMBERING_SERIES_NOT_FOUND,
                               f"no active numbering series {series_code!r}")
    series_id, prefix, suffix, pad_width = series

    counter_row = session.fetchone(
        """
        INSERT INTO numbering_counter (series_id, period_key, last_value, updated_at)
        VALUES (%(series_id)s, %(period_key)s, 1, now())
        ON CONFLICT (series_id, period_key)
        DO UPDATE SET last_value = numbering_counter.last_value + 1, updated_at = now()
        RETURNING last_value
        """,
        {"series_id": series_id, "period_key": period_key},
    )
    value = counter_row[0]
    formatted = f"{prefix}{str(value).zfill(pad_width)}{suffix}"

    issued_row = session.fetchone(
        """
        INSERT INTO numbering_issued
            (series_id, period_key, value, formatted_number, object_type, object_id, issued_by)
        VALUES (%(series_id)s, %(period_key)s, %(value)s, %(formatted)s,
                %(object_type)s, %(object_id)s, %(actor)s)
        RETURNING issued_id, issued_at
        """,
        {"series_id": series_id, "period_key": period_key, "value": value,
         "formatted": formatted, "object_type": object_type, "object_id": object_id,
         "actor": actor},
    )
    issued_id, issued_at = issued_row
    return {
        "issued_id": issued_id,
        "series_code": series_code,
        "period_key": period_key,
        "value": value,
        "formatted_number": formatted,
        "issued_at": issued_at.isoformat() if hasattr(issued_at, "isoformat") else issued_at,
    }


# ============================================================================
# Custom fields (REQ-SEC-007) -- minimal service layer. No REST endpoint is
# exposed for these in this wave: docs/WAVE2_CONTRACTS.md's frozen API
# contract does not list one, and inventing undocumented surface is exactly
# the drift the freeze exists to prevent. The schema and this service layer
# exist so the capability is real and tested; wiring a route is later work.
# ============================================================================
def define_custom_field(session: Session, *, actor: str, code: str, label: str,
                         data_type: str, applies_to: Sequence[str],
                         is_required: bool = False,
                         select_options: list[str] | None = None,
                         correlation_id: str | None = None) -> dict[str, Any]:
    field_def_id = f"CF-{uuid4().hex[:12].upper()}"
    session.execute(
        """
        INSERT INTO custom_field_def
            (field_def_id, code, label, data_type, select_options, is_required,
             is_active, created_by, updated_by)
        VALUES (%(id)s, %(code)s, %(label)s, %(data_type)s, %(select_options)s,
                %(is_required)s, true, %(actor)s, %(actor)s)
        """,
        {"id": field_def_id, "code": code, "label": label, "data_type": data_type,
         "select_options": _to_jsonb(select_options), "is_required": is_required,
         "actor": actor},
    )
    for target in applies_to:
        applicability_id = f"CFA-{uuid4().hex[:12].upper()}"
        session.execute(
            """
            INSERT INTO custom_field_applicability
                (applicability_id, field_def_id, applies_to, is_active, created_by)
            VALUES (%s, %s, %s, true, %s)
            """,
            (applicability_id, field_def_id, target, actor),
        )
    pg_audit.append(session, actor, "CREATE", "custom_field_def", field_def_id,
                     f"code={code!r} data_type={data_type!r} applies_to={list(applies_to)}",
                     correlation_id=correlation_id)
    row = session.fetchone(
        "SELECT field_def_id, code, label, data_type, select_options, is_required, "
        "is_active, version_no FROM custom_field_def WHERE field_def_id = %s",
        (field_def_id,))
    assert row is not None
    keys = ("field_def_id", "code", "label", "data_type", "select_options",
            "is_required", "is_active", "version_no")
    return dict(zip(keys, row))


def set_custom_field_value(session: Session, *, actor: str, field_code: str,
                            object_type: str, object_id: str, value: Any,
                            correlation_id: str | None = None) -> dict[str, Any]:
    field = session.fetchone(
        "SELECT field_def_id FROM custom_field_def WHERE code = %s AND is_active",
        (field_code,))
    if field is None:
        raise MasterDataError(404, "CUSTOM_FIELD_NOT_FOUND",
                               f"no active custom field {field_code!r}")
    field_def_id = field[0]

    applicable = session.fetchone(
        "SELECT 1 FROM custom_field_applicability "
        "WHERE field_def_id = %s AND applies_to = %s AND is_active",
        (field_def_id, object_type))
    if applicable is None:
        raise MasterDataError(
            422, "CUSTOM_FIELD_NOT_APPLICABLE",
            f"{field_code!r} does not apply to object type {object_type!r}")

    value_id = f"CFV-{uuid4().hex[:12].upper()}"
    session.execute(
        """
        INSERT INTO custom_field_value
            (value_id, field_def_id, object_type, object_id, value, created_by, updated_by)
        VALUES (%(id)s, %(field_def_id)s, %(object_type)s, %(object_id)s, %(value)s,
                %(actor)s, %(actor)s)
        ON CONFLICT (field_def_id, object_type, object_id)
        DO UPDATE SET value = %(value)s, updated_by = %(actor)s, updated_at = now(),
                      version_no = custom_field_value.version_no + 1
        RETURNING value_id, value, version_no
        """,
        {"id": value_id, "field_def_id": field_def_id, "object_type": object_type,
         "object_id": object_id, "value": _to_jsonb(value), "actor": actor},
    )
    pg_audit.append(session, actor, "SET_CUSTOM_FIELD", object_type, object_id,
                     f"field={field_code!r}", correlation_id=correlation_id)
    return {"field_code": field_code, "object_type": object_type,
            "object_id": object_id, "value": value}


def _to_jsonb(value: Any) -> Any:
    """psycopg3 does NOT adapt a plain Python `dict`/`list` to `jsonb`
    automatically -- it must be wrapped in `Jsonb(...)`, or the driver raises
    `CannotAdaptType` for anything but a `str` payload. `None` is passed
    through unwrapped so the column gets a real SQL NULL, not a jsonb `null`
    literal."""
    return None if value is None else Jsonb(value)
