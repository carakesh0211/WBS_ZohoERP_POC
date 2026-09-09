-- 024_audit_anchor_integrity.sql
-- Constrains audit_anchor so the anchors the Wave 8 writer produces cannot be degenerate, and records that the table finally has a writer.
--
-- WHY THIS FILE EXISTS AT ALL
-- ==========================================================================
--
-- `001_foundation.sql:250-256` created `audit_anchor`, `001:273-277` made it
-- append-only by trigger, and `004_identity_scope.sql:96` revoked UPDATE and
-- DELETE on it from `capex_app`. Three files protected a table that no code
-- ever wrote a row into. The consequence was not cosmetic:
-- `pg/audit.py::verify_chain` says so in its own returned payload --
--
--     "Deletion of an entire stream is detectable only against the daily
--      anchors, which are not yet written."
--
-- -- and `tests/test_pg_audit.py::test_tail_truncation_is_NOT_detectable_from
-- _the_stream_alone` pins that gap as current behaviour. A hash chain is a
-- statement about the rows you are looking at; deleting the last k entries of
-- a stream, or the whole stream, leaves a perfectly self-consistent database.
-- The anchor is the external record that makes those two cases answerable.
--
-- Wave 8 supplies the missing writer and verifier in `app/backend/pg/audit.py`
-- (`write_anchor`, `verify_anchors`). This migration supplies the three
-- schema-level guarantees that writer's verification depends on, none of which
-- can be added to 001 or 004: the checksum recorded in `schema_migrations` is
-- `sha256` over the WHOLE FILE, comments included --
--
--     body = self.path.read_bytes().replace(b"\r\n", b"\n")
--     return hashlib.sha256(body).hexdigest()
--                                     -- migrate_pg.py:138-142
--
-- so adding one commented line to 001 moves its checksum exactly as far as
-- rewriting its DDL would, `assert_schema_current` refuses to BOOT with
-- `schema drift` (migrate_pg.py:933-937), and `upgrade()` refuses to proceed
-- with `Never edit an applied migration -- add a new one.`
-- (migrate_pg.py:876-880). Neither has a repair path. This is the argument
-- 020's, 021's and 022's headers set out at length and it is not restated
-- further here.
--
--
-- WHAT IS ADDED, AND WHY EACH ONE IS LOAD-BEARING
-- ==========================================================================
--
-- (1) `ck_audit_anchor_hash_present`. `anchor_hash` is `text NOT NULL`, and
--     `NOT NULL` admits the empty string. An anchor row carrying `''` would
--     satisfy every existing constraint while proving nothing at all, and --
--     worse -- the NEXT anchor chains from it, so one empty hash silently
--     becomes the `prev_anchor_hash` a whole chain is anchored on. A
--     verification that recomputes hashes cannot distinguish "this anchor is
--     empty because nothing was ever computed" from "this anchor is empty
--     because somebody emptied it": both are `''`. Refusing it at write time
--     is the only place the distinction still exists.
--
-- (2) `ck_audit_anchor_stream_heads_is_object`. `stream_heads` is `jsonb NOT
--     NULL`, and jsonb happily stores `'null'::jsonb`, `'[]'`, `'"x"'` or
--     `'5'` -- all NOT NULL, none of them a map of stream_key -> head. The
--     verifier iterates `stream_heads.items()`; a scalar or an array there
--     does not raise, it iterates NOTHING, and "no stream was anchored" reads
--     identically to "no stream is missing". That is the same shape of defect
--     as an empty scope compiling to TRUE: a degenerate value silently
--     meaning "no findings". The type is asserted where it is stored.
--
--     `'{}'::jsonb` -- an object with no streams -- IS permitted, and
--     deliberately: a database with no audit rows yet has genuinely no heads
--     to record, and refusing to anchor it would mean the first anchor could
--     only ever be written after the first business event.
--
-- (3) `ux_audit_anchor_hash`. `anchor_date` is already the primary key, so
--     one day cannot be anchored twice. This closes the other direction: two
--     DIFFERENT dates cannot carry the same `anchor_hash`. Because the frozen
--     payload is
--
--         prev_anchor_hash|anchor_date|canonical_json(stream_heads)
--
--     and `anchor_date` is inside it, two distinct dates producing one hash
--     means either a collision or -- far more likely -- a row copied from
--     another day to make a gap in the chain look continuous. Copying is
--     precisely the attack the `prev_anchor_hash` link exists to defeat, and
--     an index refuses it at write time rather than at audit time.
--
-- (4) `COMMENT ON TABLE`. The table's own documentation said, in
--     `001:247-249`, what anchors were FOR. It could not say what wrote them,
--     because nothing did. It can now, and an operator reading the schema is
--     the person who most needs to know that these rows are produced by a job
--     that has to actually run.
--
-- NOTHING HERE IS DESTRUCTIVE, and nothing here changes an existing row: three
-- constraints and a comment. `audit_anchor` is empty in every database this
-- has ever run against (there was no writer), so the CHECKs validate against
-- nothing; were a database to carry a hand-inserted anchor -- as
-- `tests/test_pg_constraints.py::test_audit_anchor_rejects_update_and_delete`
-- creates inside its own test transaction -- a well-formed one satisfies both.
--
-- RLS: `audit_anchor` remains deliberately unscoped, and this migration does
-- not change that. It carries no entity, plant, location or project column and
-- no join to one; it is a database-wide integrity record, classified in
-- `app/backend/pg/scope_inventory.py` with that reason. Scoping it would mean
-- an anchor that only some principals can verify, which is the opposite of
-- what an anchor is for.

BEGIN;

ALTER TABLE audit_anchor
    ADD CONSTRAINT ck_audit_anchor_hash_present
    CHECK (anchor_hash <> '');

ALTER TABLE audit_anchor
    ADD CONSTRAINT ck_audit_anchor_stream_heads_is_object
    CHECK (jsonb_typeof(stream_heads) = 'object');

CREATE UNIQUE INDEX ux_audit_anchor_hash ON audit_anchor (anchor_hash);

COMMENT ON TABLE audit_anchor IS
    'Daily heads of every audit_log stream, hash-chained to the previous day. '
    'Written by app.backend.pg.audit.write_anchor and checked by '
    'verify_anchors; append-only by trigger (001) with UPDATE/DELETE revoked '
    'from capex_app (004). This is the ONLY record against which deletion of '
    'an entire audit stream, or truncation of a stream tail, is detectable -- '
    'verify_chain cannot see either, by construction. Anchors detect only what '
    'they were written to see: a day on which the writer did not run is a day '
    'with no evidence.';

COMMENT ON COLUMN audit_anchor.stream_heads IS
    'jsonb object, stream_key -> {"seq": int, "entries": int, "entry_hash": text}. '
    'Constrained to an object: the verifier iterates it, and a scalar or array '
    'would iterate nothing and report no findings.';

COMMIT;

-- ROLLBACK:
--   BEGIN;
--   COMMENT ON COLUMN audit_anchor.stream_heads IS NULL;
--   COMMENT ON TABLE audit_anchor IS NULL;
--   DROP INDEX IF EXISTS ux_audit_anchor_hash;
--   ALTER TABLE audit_anchor
--       DROP CONSTRAINT IF EXISTS ck_audit_anchor_stream_heads_is_object;
--   ALTER TABLE audit_anchor
--       DROP CONSTRAINT IF EXISTS ck_audit_anchor_hash_present;
--   DELETE FROM schema_migrations WHERE version = '024';
--   COMMIT;
