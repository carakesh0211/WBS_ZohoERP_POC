-- 014_procurement_corrections.sql
-- Six defects in 013 and six controls the SQLite build has that PostgreSQL does not.
--
-- WHAT THIS IS
--
-- `docs/WAVE6_MIGRATION_014_SPEC.md`, implemented. That document was approved
-- by the product owner on 2026-09-07 (Part One) and 2026-09-08 (Part Two), and
-- every section below names the spec item it closes. Where the spec is wrong
-- or under-specified this file says so IN THE PLACE THE DEVIATION HAPPENS
-- rather than in a report nobody reads next to the SQL.
--
-- THIS MIGRATION IS ADDITIVE AND CORRECTIVE. IT DOES NOT MODIFY 013.
--
-- 013 is already applied wherever this build has run, and `schema_migrations`
-- has recorded its SHA-256. Editing it would change that checksum and make
-- every existing database report drift and refuse to boot
-- (`migrate_pg.assert_schema_current`). Everything here is an ALTER, a CREATE,
-- or a DROP of a named object 013 created and this file replaces.
--
--
-- FOUR OBJECTS ARE DROPPED, AND THREE OF THEM ARE NOT INDEXES
--
-- The spec's migration-safety clause says "drop the incorrect INDEXES by their
-- exact names: ux_grn_number, ux_bill_number, ux_grn_line_external,
-- ix_purchase_order_pr". Three of those four are declared in 013 as TABLE
-- CONSTRAINTS (`CONSTRAINT ux_grn_number UNIQUE (grn_number)` and its two
-- siblings), not as `CREATE UNIQUE INDEX`. `DROP INDEX ux_grn_number` fails
-- with "cannot drop index ux_grn_number because constraint ux_grn_number on
-- table grn requires it"; the correct statement is `ALTER TABLE grn DROP
-- CONSTRAINT ux_grn_number`. Only `ix_purchase_order_pr` is a real index.
--
-- The four names are exactly the spec's. The STATEMENTS are the ones that
-- work. This is deviation 1 of 4 and is reported.
--
--
-- TWO MORE INDEXES ARE DROPPED THAN THE SPEC NAMES, AND WITHOUT THEM THE
-- CORRECTION WOULD HAVE NO EFFECT
--
-- D1 adds `ux_grn_external_identity` / `ux_bill_external_identity` on
-- `(connection_id, external_source, external_id)` so that "the same external
-- document under two Zoho organisations does not collide". 013's
-- `ux_grn_external` / `ux_bill_external` are UNIQUE on `(external_source,
-- external_id)` ESTATE-WIDE. Leaving them in place means the second
-- organisation still collides -- on the older index -- and the new one changes
-- nothing at all. They are therefore dropped too.
--
-- Dropping them would ordinarily WEAKEN replay idempotency for a locally
-- created or legacy-mirrored row whose `connection_id` is NULL, because
-- PostgreSQL's default treats NULLs as distinct and every such row would stop
-- colliding with itself. `NULLS NOT DISTINCT` is what makes the replacement
-- strictly stronger rather than merely different: with `connection_id` NULL on
-- both sides the new index behaves EXACTLY as 013's two-column index did, and
-- with two different non-null connection ids it correctly does not collide.
--
-- `pg/procurement.py`'s two `ON CONFLICT (external_source, external_id)`
-- targets move to the three columns in the same change. An `ON CONFLICT`
-- inference that matches no index is an error, not a silent fallback.
--
-- This is deviation 2 of 4 and is reported. `ux_po_external` and
-- `ux_po_line_external` carry the same estate-wide shape and are NOT touched:
-- the spec's D1 is scoped to `grn` and `bill`, `pg/procurement.py`
-- (`_purchase_order`) already treats a duplicate external PO id as an
-- AMBIGUOUS_PURCHASE_ORDER refusal rather than a collision, and widening the
-- change to the purchase-order chain on this migration's own initiative is the
-- improvisation this project bans. Recorded as a residual gap.
--
--
-- D1 NAMES TWO COLUMNS FOR `ux_grn_number_scoped` THAT IT NEVER DECLARES
--
-- `ux_grn_number_scoped UNIQUE NULLS NOT DISTINCT (entity_id,
-- numbering_series_id, period_key, grn_number)` -- but D1's "new columns on
-- grn and bill" list is `connection_id` and `entity_id` only.
-- `numbering_series_id` and `period_key` do not exist on `grn` in 013 and are
-- added here. They are the fiscal/series scope the spec's own mechanism table
-- points at (`numbering_series.series_id` + `numbering_counter.period_key`,
-- 005_master_data.sql:54,76). This is deviation 3 of 4 and is reported.
--
--
-- `external_status_raw` GOES ON SEVEN TABLES, NOT EIGHT
--
-- D4's defect statement counts eight tables; its CORRECTION says "every
-- procurement table that can REPRESENT A MIRRORED EXTERNAL ROW". `pr_line`
-- carries no external identity of any kind -- no `external_id`, no
-- `line_external_id`, nothing -- so a column on it could never be populated by
-- anything, and a column that is structurally always NULL is not evidence, it
-- is furniture. The other seven get it. This is deviation 4 of 4 and is
-- reported.
--
--
-- WHY `SET LOCAL capex.read_all` IS THE FIRST STATEMENT
--
-- The preflight and the backfills below READ and WRITE 013's eight tables, and
-- all eight carry `FORCE ROW LEVEL SECURITY`. FORCE means the table's OWNER
-- does not bypass its policies -- and in production the owner is the deploy
-- identity that runs migrations, which is deliberately not a superuser. With
-- no `capex.*` session settings applied, `capex_scope_permits` returns false
-- for every row, so the preflight would report a clean database, the backfills
-- would update nothing, and `SET NOT NULL` would then fail on rows the
-- migration could not see.
--
-- In CI this does not arise, because the fixture role IS a superuser and
-- superusers bypass RLS unconditionally. That is exactly the shape of failure
-- that appears in one environment only, which is why it is stated here rather
-- than discovered there. `SET LOCAL` is scoped to this transaction and expires
-- at the COMMIT below; it does not leak into the session or into any later
-- migration.
--
--
-- THE PREFLIGHT REFUSES; IT NEVER RESOLVES
--
-- Every uniqueness rule this migration introduces is narrower than what 013
-- allowed, so existing data can contradict it. The DO block below finds every
-- such row FIRST, names it in a NOTICE, and then RAISES. It never deletes,
-- merges, renumbers or "picks the newest": an operator is told which rows to
-- fix and fixes them, because a migration that silently resolves an ambiguity
-- in a financial document is a migration that decided which bill was real.
--
-- Two things it does do, and both are population rather than resolution:
--
--   * `grn.entity_id` / `bill.entity_id` are BACKFILLED from the join the
--     table already has (`grn -> purchase_order -> project -> entity`,
--     `bill -> project -> entity`). Adding a NOT NULL column to a populated
--     table requires a value; that value is derived from the row itself and is
--     the only value it could have.
--   * `bill_line.line_no` is populated where NULL, as
--     `row_number() OVER (PARTITION BY bill_id ORDER BY bill_line_id)`. This
--     is the INITIAL value of a column that did not exist a statement ago, not
--     a renumbering of one that did. It matters because `line_fingerprint`
--     includes the ordinal, and every existing line sharing a NULL ordinal
--     would otherwise fingerprint alike.
--
--
-- MONEY
--
-- `pr_reservation.amount_paise` is `bigint`, integer paise, and it STARTS its
-- declaration line. `migrate_pg._PAISE_COLUMN_RE` is anchored at `^` against
-- each stripped field of a `CREATE TABLE` body; a paise column the parser
-- cannot see is a money column whose bigint-ness never verifies, and drift to
-- `numeric(18,2)` then certifies as adopted.
--
--
-- EVERY CONSTRAINT AND INDEX IS NAMED
--
-- `migrate_pg._named_constraints_by` reports only constraints declared with an
-- explicit `CONSTRAINT name`; an anonymous CHECK has no `pg_constraint.conname`
-- to look up and therefore certifies as adopted while absent. Not one
-- constraint below is anonymous, and this migration extends the runner's
-- adoption verification to `ALTER TABLE ... ADD COLUMN` and
-- `ALTER TABLE ... ADD CONSTRAINT`, neither of which the CREATE-TABLE-only
-- parsers could see at all.
--
--
-- ARGUMENT ORDER MATTERS AND IS EASY TO GET WRONG
--
-- `capex_scope_permits(p_entity_id, p_plant_id, p_location_id, p_project_id)`
-- -- 004. All four parameters are `text`, so PostgreSQL accepts ANY order
-- silently. 011 put `project_id` in the LOCATION slot and the project
-- dimension went unenforced for a whole wave. Every call below names its
-- arguments in the same left-to-right order as the `project` columns they come
-- from.

BEGIN;

-- The migration runs as the table owner, and 013's tables are FORCE RLS. See
-- the header. Transaction-scoped; expires at COMMIT.
SET LOCAL capex.read_all = 'true';

-- ============================================================== PREFLIGHT
-- Runs BEFORE any DDL. Names every offending row, then refuses.
--
-- It reads TODAY'S columns, which is the point: `numbering_series_id`,
-- `period_key`, `vendor_id` and `line_no` do not exist yet, so every existing
-- row lands in their NULL group and the effective rule for existing data is
-- the narrower one. On a RE-RUN against a database already at 014 -- reachable
-- only if the `schema_migrations` row was lost -- those columns DO exist and
-- are ignored here, so two receipts legitimately separated by distinct
-- numbering series would be reported. That is a false positive, it is loud,
-- and it names the rows; it is the safe direction, and the alternative
-- (dynamic SQL branching on `information_schema`) makes the one check that
-- must be obviously correct the least readable thing in the file.
DO $preflight$
DECLARE
    offending  record;
    problems   integer := 0;
    reported   integer := 0;
BEGIN
    RAISE NOTICE '014 preflight: checking existing rows against the narrower rules this migration introduces.';

    -- D1a. `ux_grn_number_scoped` groups on (entity, series, period, number)
    -- with NULLS NOT DISTINCT. `numbering_series_id` and `period_key` do not
    -- exist yet, so every existing row lands in the NULL/NULL group and the
    -- effective rule for TODAY'S data is (entity_id, grn_number).
    FOR offending IN
        SELECT p.entity_id, g.grn_number, count(*) AS n,
               string_agg(g.grn_id, ', ' ORDER BY g.grn_id) AS ids
        FROM grn g
        JOIN purchase_order po ON po.po_id = g.po_id
        JOIN project p ON p.project_id = po.project_id
        GROUP BY p.entity_id, g.grn_number
        HAVING count(*) > 1
    LOOP
        problems := problems + 1;
        RAISE NOTICE 'PREFLIGHT ux_grn_number_scoped: entity % already carries % goods receipts numbered % (grn_id: %). Renumber all but one, or give them distinct numbering_series_id/period_key values, before applying 014.',
            offending.entity_id, offending.n, offending.grn_number, offending.ids;
    END LOOP;

    -- D1b. `ux_bill_number_scoped` on (entity_id, vendor_key,
    -- bill_number_normalised). `vendor_id` does not exist yet, so `vendor_key`
    -- is `vendor_name` for every existing row.
    FOR offending IN
        SELECT p.entity_id, b.vendor_name,
               capex_normalise_text(b.bill_number) AS normalised,
               count(*) AS n,
               string_agg(b.bill_id, ', ' ORDER BY b.bill_id) AS ids
        FROM bill b
        JOIN project p ON p.project_id = b.project_id
        GROUP BY p.entity_id, b.vendor_name, capex_normalise_text(b.bill_number)
        HAVING count(*) > 1
    LOOP
        problems := problems + 1;
        RAISE NOTICE 'PREFLIGHT ux_bill_number_scoped: entity % already carries % bills from vendor % whose numbers normalise to % (bill_id: %). Two different vendors may share a bill number; ONE vendor may not use one twice in one entity.',
            offending.entity_id, offending.n, offending.vendor_name,
            offending.normalised, offending.ids;
    END LOOP;

    -- D1c/D5. The two external-identity indexes and `ux_grn_line_external_v2`
    -- all switch to NULLS NOT DISTINCT, which is STRICTLY NARROWER than the
    -- default: rows that did not collide because a column was NULL now do.
    FOR offending IN
        SELECT g.external_source, g.external_id, count(*) AS n,
               string_agg(g.grn_id, ', ' ORDER BY g.grn_id) AS ids
        FROM grn g
        WHERE g.external_id IS NOT NULL
        GROUP BY g.external_source, g.external_id
        HAVING count(*) > 1
    LOOP
        problems := problems + 1;
        RAISE NOTICE 'PREFLIGHT ux_grn_external_identity: % goods receipts share external_source % / external_id % under a NULL connection_id (grn_id: %). Set connection_id on all but one before applying 014.',
            offending.n, offending.external_source, offending.external_id, offending.ids;
    END LOOP;

    FOR offending IN
        SELECT b.external_source, b.external_id, count(*) AS n,
               string_agg(b.bill_id, ', ' ORDER BY b.bill_id) AS ids
        FROM bill b
        WHERE b.external_id IS NOT NULL
        GROUP BY b.external_source, b.external_id
        HAVING count(*) > 1
    LOOP
        problems := problems + 1;
        RAISE NOTICE 'PREFLIGHT ux_bill_external_identity: % bills share external_source % / external_id % under a NULL connection_id (bill_id: %).',
            offending.n, offending.external_source, offending.external_id, offending.ids;
    END LOOP;

    FOR offending IN
        SELECT gl.po_line_id, gl.receive_external_id, gl.line_external_id,
               count(*) AS n,
               string_agg(gl.grn_line_id, ', ' ORDER BY gl.grn_line_id) AS ids
        FROM grn_line gl
        GROUP BY gl.po_line_id, gl.receive_external_id, gl.line_external_id
        HAVING count(*) > 1
    LOOP
        problems := problems + 1;
        RAISE NOTICE 'PREFLIGHT ux_grn_line_external_v2: % receive lines share po_line % / receive % / line % and would collide once NULLs stop being distinct (grn_line_id: %). This is the duplicated-receipt shape D5 exists to stop; each surviving line needs a distinct line_no.',
            offending.n, offending.po_line_id, offending.receive_external_id,
            offending.line_external_id, offending.ids;
    END LOOP;

    -- D6. `ux_purchase_order_pr` makes "a PR converts exactly once" structural.
    FOR offending IN
        SELECT po.pr_id, count(*) AS n,
               string_agg(po.po_id, ', ' ORDER BY po.po_id) AS ids
        FROM purchase_order po
        WHERE po.pr_id IS NOT NULL
        GROUP BY po.pr_id
        HAVING count(*) > 1
    LOOP
        problems := problems + 1;
        RAISE NOTICE 'PREFLIGHT ux_purchase_order_pr: purchase request % has already been converted into % purchase orders (po_id: %). Nothing here decides which one is real.',
            offending.pr_id, offending.n, offending.ids;
    END LOOP;

    -- D2. `ux_bill_line_external` and `ux_bill_line_fingerprint` CANNOT be
    -- violated by existing data, and saying that plainly is better than
    -- running a query that looks like a check and can never fire -- which is
    -- the "vacuously False" trap 011's header names.
    --
    --   * `external_line_id` is a column this migration ADDS, so it is NULL on
    --     every existing row and `ux_bill_line_external`'s partial predicate
    --     (`WHERE external_line_id IS NOT NULL`) matches none of them.
    --   * `line_fingerprint` includes the ordinal, and the backfill below
    --     assigns ordinals with `row_number()` PARTITIONED BY `bill_id`, which
    --     is injective within a bill. Two rows on one bill therefore cannot
    --     share a fingerprint however alike they are otherwise.
    --
    -- What IS checked is the backfill's own premise, because that is the part
    -- a future edit could break: every bill line must end up with an ordinal,
    -- and no two on one bill may share one. A window function changed to
    -- something non-injective would fire this instead of producing a
    -- constraint violation three statements later.
    FOR offending IN
        WITH numbered AS (
            SELECT bl.bill_id, bl.bill_line_id,
                   row_number() OVER (PARTITION BY bl.bill_id
                                      ORDER BY bl.bill_line_id) AS line_no
            FROM bill_line bl
        )
        SELECT bill_id, line_no, count(*) AS n,
               string_agg(bill_line_id, ', ' ORDER BY bill_line_id) AS ids
        FROM numbered
        GROUP BY bill_id, line_no
        HAVING count(*) > 1
    LOOP
        problems := problems + 1;
        RAISE NOTICE 'PREFLIGHT bill_line.line_no backfill: % lines on bill % would be assigned ordinal % (bill_line_id: %). The ordinal is an input to line_fingerprint, so duplicates there become duplicate fingerprints and ux_bill_line_fingerprint refuses the index.',
            offending.n, offending.bill_id, offending.line_no, offending.ids;
    END LOOP;

    -- D3. The three status CHECKs make previously acceptable rows invalid, so
    -- what is rejected is REPORTED rather than discovered as a 23514 at 3am.
    -- The permitted sets are restated here verbatim from the constraints
    -- below; see the D3 section for where each value comes from.
    FOR offending IN
        SELECT 'purchase_order' AS tbl, po.po_id AS id, po.status AS status
        FROM purchase_order po
        WHERE po.status NOT IN ('Draft', 'Submitted', 'Approved', 'Released',
                                'Fully Committed', 'Partially Actualised',
                                'Fully Actualised', 'Cancelled', 'Closed')
        UNION ALL
        SELECT 'grn', g.grn_id, g.status FROM grn g
        WHERE g.status NOT IN ('Approved', 'Void')
        UNION ALL
        SELECT 'bill', b.bill_id, b.status FROM bill b
        WHERE b.status NOT IN ('Approved', 'Void')
        ORDER BY 1, 2
    LOOP
        problems := problems + 1;
        RAISE NOTICE 'PREFLIGHT status CHECK: %.% carries status %, which is outside the permitted set. It is not mapped and 014 will not guess what it means.',
            offending.tbl, offending.id, offending.status;
    END LOOP;

    IF problems > 0 THEN
        RAISE EXCEPTION
            '014 preflight refused: % ambiguity/violation(s) reported above. NOTHING has been changed. Fix the named rows and re-run. This migration does not delete, merge, renumber or choose between financial documents.',
            problems
            USING ERRCODE = 'raise_exception';
    END IF;

    -- Not a "0 problems" line only: an operator needs to see that the checks
    -- ran at all against a database whose procurement chain is empty.
    SELECT count(*) INTO reported FROM purchase_order;
    RAISE NOTICE '014 preflight: clean. % purchase order(s) inspected.', reported;
END
$preflight$;


-- ================================================== PART ONE, D1: identity
-- The authoritative idempotency key is EXTERNAL IDENTITY, never the
-- human-readable number. `bill_number` and `grn_number` are preserved
-- unmodified for display and audit; normalisation exists only to make identity
-- robust and never replaces what the vendor actually printed.

ALTER TABLE grn ADD COLUMN connection_id text;
ALTER TABLE grn ADD COLUMN entity_id text;
ALTER TABLE grn ADD COLUMN numbering_series_id text;
ALTER TABLE grn ADD COLUMN period_key text;

ALTER TABLE bill ADD COLUMN connection_id text;
ALTER TABLE bill ADD COLUMN entity_id text;
ALTER TABLE bill ADD COLUMN vendor_id text;

-- Denormalised because neither table reaches an entity except through a join,
-- and a uniqueness constraint cannot follow one. Backfilled from that same
-- join, which is the only value the column could hold.
UPDATE grn g
   SET entity_id = p.entity_id
  FROM purchase_order po
  JOIN project p ON p.project_id = po.project_id
 WHERE po.po_id = g.po_id
   AND g.entity_id IS NULL;

UPDATE bill b
   SET entity_id = p.entity_id
  FROM project p
 WHERE p.project_id = b.project_id
   AND b.entity_id IS NULL;

ALTER TABLE grn ALTER COLUMN entity_id SET NOT NULL;
ALTER TABLE bill ALTER COLUMN entity_id SET NOT NULL;

ALTER TABLE grn
    ADD CONSTRAINT fk_grn_connection
        FOREIGN KEY (connection_id) REFERENCES integration_connection (connection_id),
    ADD CONSTRAINT fk_grn_entity
        FOREIGN KEY (entity_id) REFERENCES entity (entity_id),
    ADD CONSTRAINT fk_grn_numbering_series
        FOREIGN KEY (numbering_series_id) REFERENCES numbering_series (series_id);

ALTER TABLE bill
    ADD CONSTRAINT fk_bill_connection
        FOREIGN KEY (connection_id) REFERENCES integration_connection (connection_id),
    ADD CONSTRAINT fk_bill_entity
        FOREIGN KEY (entity_id) REFERENCES entity (entity_id),
    -- NULLABLE. A mirrored bill may arrive before its vendor is mastered, and
    -- refusing it until a `vendor_master` row exists would be a silent drop of
    -- an accounting document.
    ADD CONSTRAINT fk_bill_vendor
        FOREIGN KEY (vendor_id) REFERENCES vendor_master (vendor_id);

-- A RESIDUAL, NAMED RATHER THAN LEFT TO BE DISCOVERED.
--
-- `fk_grn_entity` and `fk_bill_entity` say the entity EXISTS. Neither says it
-- is the entity the document's own project belongs to, and the composite-FK
-- pattern 013 uses everywhere else cannot express that here:
--
--   * `bill (project_id, entity_id) -> project (project_id, entity_id)` would
--     need a UNIQUE on `project (project_id, entity_id)`, which 002 does not
--     declare -- its keys are `project_id` and `(entity_id, capex_code)`.
--   * `grn` reaches an entity only through `po -> project`, and
--     `purchase_order` carries no `entity_id`, so there is no two-column
--     target to bind against at all.
--
-- Adding a UNIQUE to `project` and a composite FK to `bill` would close half
-- of it and leave `grn`'s half open, which reads as enforcement where there is
-- none -- the failure shape 013's own header warns about twice. So NEITHER is
-- added, and the invariant is held where it is actually decided: both writers
-- (`pg/procurement.py::_mirror_grn_header` and `::mirror_bill`) SELECT the
-- entity from the row's own join and never accept it from a caller. That is a
-- code-level guarantee, not a declarative one, and it is recorded as such.

-- GENERATED ... STORED, not application-maintained, for the reason
-- 005_master_data.sql gives `capex_normalise_text` itself: a generated column
-- is always consistent with its source REGARDLESS OF WHICH CODE PATH WROTE THE
-- ROW, so identity can never drift out of step with a helper somebody forgot
-- to call. `capex_normalise_text` is IMMUTABLE, which is what makes it legal
-- here.
ALTER TABLE bill ADD COLUMN bill_number_normalised text
    GENERATED ALWAYS AS (capex_normalise_text(bill_number)) STORED;

-- `vendor_name` is NOT NULL in 013, so `vendor_key` is never NULL: the fallback
-- is total, not a hope.
ALTER TABLE bill ADD COLUMN vendor_key text
    GENERATED ALWAYS AS (COALESCE(vendor_id, vendor_name)) STORED;

-- 013's estate-wide numbers. Dropped as CONSTRAINTS, which is what they are.
ALTER TABLE grn DROP CONSTRAINT IF EXISTS ux_grn_number;
ALTER TABLE bill DROP CONSTRAINT IF EXISTS ux_bill_number;

-- ...and 013's estate-wide external identities, superseded. See the header:
-- without this the two-organisation case still collides on the older index and
-- the new one would be decoration.
DROP INDEX IF EXISTS ux_grn_external;
DROP INDEX IF EXISTS ux_bill_external;

-- PARTIAL on `external_id IS NOT NULL`, because a locally created document has
-- no external identity and must not be forced to invent one.
--
-- NULLS NOT DISTINCT is what makes this a STRICT REPLACEMENT for 013's
-- two-column index rather than a weakening: with `connection_id` NULL on both
-- rows the group is identical to the old one, and with two different
-- organisations it correctly is not.
CREATE UNIQUE INDEX ux_grn_external_identity
    ON grn (connection_id, external_source, external_id)
    NULLS NOT DISTINCT
    WHERE external_id IS NOT NULL;

CREATE UNIQUE INDEX ux_bill_external_identity
    ON bill (connection_id, external_source, external_id)
    NULLS NOT DISTINCT
    WHERE external_id IS NOT NULL;

-- NULLS NOT DISTINCT is LOAD-BEARING on both of the next two, and for the same
-- reason it is on D5 below. PostgreSQL's default treats NULLs as distinct, so
-- without it two goods receipts in one entity with no numbering series and the
-- same number would NOT collide -- the constraint would be absent in precisely
-- the ordinary case, which is the defect shape, not a corner of it.
CREATE UNIQUE INDEX ux_grn_number_scoped
    ON grn (entity_id, numbering_series_id, period_key, grn_number)
    NULLS NOT DISTINCT;

CREATE UNIQUE INDEX ux_bill_number_scoped
    ON bill (entity_id, vendor_key, bill_number_normalised)
    NULLS NOT DISTINCT;

CREATE INDEX ix_grn_entity ON grn (entity_id);
CREATE INDEX ix_bill_entity ON bill (entity_id);
CREATE INDEX ix_bill_vendor ON bill (vendor_id) WHERE vendor_id IS NOT NULL;


-- ============================================ PART ONE, D2: bill-line replay
-- 013 gave `bill_line` no unique index on any external identity, unlike
-- `grn_line`. Replay idempotency could not be delegated to the database, and
-- since 013 correctly REVOKES DELETE from `capex_app`, a duplicated bill line
-- was unrecoverable.

ALTER TABLE bill_line ADD COLUMN external_line_id text;
ALTER TABLE bill_line ADD COLUMN line_no integer;

-- The initial value of a column that did not exist a statement ago. See the
-- header: this is population, not renumbering. Deterministic, so two runs of
-- this migration against the same data produce the same ordinals.
UPDATE bill_line bl
   SET line_no = n.line_no
  FROM (SELECT bill_line_id,
               row_number() OVER (PARTITION BY bill_id
                                  ORDER BY bill_line_id) AS line_no
        FROM bill_line) n
 WHERE n.bill_line_id = bl.bill_line_id
   AND bl.line_no IS NULL;

-- DETERMINISTIC, never random, never the mutable description alone, and never
-- cleaned up by DELETE. The inputs are fixed by the spec so a later reader can
-- verify a stored value: `po_line_external_id`, `amount_paise`, `quantity`,
-- `line_no`.
--
-- GENERATED rather than application-computed, for the same reason
-- `bill_number_normalised` is: a fingerprint a code path forgot to write is a
-- fingerprint that does not constrain anything.
--
-- `trim_scale` normalises `1` and `1.0` to the same numeric before the text
-- cast. Without it the digest depends on how many trailing zeros a source
-- happened to send, which is exactly the accidental instability a fingerprint
-- must not have. It is immutable, and PostgreSQL 13+; CI runs postgres:16.
--
-- The separator is US (0x1f), a character no Zoho identifier contains, so
-- ('A','BC') and ('AB','C') cannot digest alike -- the same reasoning as
-- `pg/procurement.py::derived_id`.
ALTER TABLE bill_line ADD COLUMN line_fingerprint text
    GENERATED ALWAYS AS (
        md5(COALESCE(po_line_external_id, '')
            || E'\x1f' || amount_paise::text
            || E'\x1f' || trim_scale(quantity)::text
            || E'\x1f' || COALESCE(line_no::text, ''))
    ) STORED;

-- Where Zoho supplies a stable line id, THAT is the identity.
CREATE UNIQUE INDEX ux_bill_line_external
    ON bill_line (bill_id, external_line_id)
    WHERE external_line_id IS NOT NULL;

-- Where it does not, the fingerprint is. Ordinal is one of its inputs because
-- two GENUINELY DISTINCT lines may be identical in every other field -- a
-- legitimate case that must NOT collapse into one row.
CREATE UNIQUE INDEX ux_bill_line_fingerprint
    ON bill_line (bill_id, line_fingerprint)
    WHERE external_line_id IS NULL AND line_fingerprint IS NOT NULL;


-- ================================== PART ONE, D3: canonical status separated
-- `purchase_order.status`, `grn.status` and `bill.status` carried NO CHECK, so
-- any string could be written -- including a raw unmapped Zoho value, which is
-- precisely the guess C17 forbids, with nothing in the schema to refuse it.
-- `compute_ledger` releases commitment on `status IN ('Cancelled','Closed')`,
-- so a typo'd 'cancelled' holds commitment FOREVER, silently.
--
-- WHERE EACH PERMITTED SET COMES FROM. Read from the registries and from the
-- code at implementation time, never typed from memory, and each value has a
-- named source:
--
--   purchase_order.status
--     Draft                  013's own DEFAULT; C3 DRAFT; C17 ERP raw 'draft'
--     Submitted              C3 SUBMITTED; C17 ERP raw 'pending_approval'
--                            (row present, INACTIVE pending Phase 0B-2, so the
--                            state is real and only its Zoho spelling is
--                            unverified)
--     Approved               C3 APPROVED; C17 ERP raw 'approved' (same)
--     Released               C3 RELEASED; C17 ERP raw 'open'
--     Fully Committed        C3 FULLY_COMMITTED; seeded by `db.py::seed`
--     Partially Actualised   C3 PARTIALLY_ACTUALISED; seeded by `db.py::seed`
--     Fully Actualised       C3 FULLY_ACTUALISED; C17 ERP raw 'billed'; seeded
--     Closed                 C3 CLOSED; C17 ERP raw 'cancelled'; written by
--                            `services.close_po`
--     Cancelled              NOT A C3 LABEL. Written by `services.cancel_po`
--                            and named by `domain.COMMITMENT_RELEASING_STATES`.
--                            Admitted BECAUSE the domain requires it: excluding
--                            it would make half of that frozen set unwritable
--                            and a purchase order uncancellable, which is a
--                            control REGRESSION dressed as a tightening. The
--                            divergence from C3 is 013's and the POC's, not
--                            this migration's, and it is recorded rather than
--                            smoothed over.
--
--   grn.status  -- 'Approved' (013's DEFAULT, the only value the POC or
--     `_mirror_grn_header` ever writes) and 'Void' (the only value
--     `compute_ledger` names: `WHERE g.status <> 'Void'`). 'Void' is a C17
--     accounting_status value, not a C3 label; same divergence, same reason.
--     Nothing else is admitted, because nothing else is evidenced anywhere in
--     this repository, and a third value here would be this migration
--     author's opinion.
--
--   bill.status -- 'Approved' (013's DEFAULT and the only written value) and
--     'Void' (013 gives the table `voided_by`/`voided_at`/`void_reason`, so a
--     voided bill is a represented state and `status` is where it lands).
--     `bill.accounting_status` already carries the accounting meaning and is
--     already constrained by 013; C17's ERP `bill.status` block is
--     ACCOUNTING_STATUS_ONLY with `maps_to` null on every row, i.e. it says
--     positively that a bill's raw status has NO business-status meaning. So
--     this column is local, and its evidenced set is two values.

ALTER TABLE purchase_order
    ADD CONSTRAINT ck_purchase_order_status CHECK (
        status IN ('Draft', 'Submitted', 'Approved', 'Released',
                   'Fully Committed', 'Partially Actualised',
                   'Fully Actualised', 'Cancelled', 'Closed')
    );

ALTER TABLE grn
    ADD CONSTRAINT ck_grn_status CHECK (status IN ('Approved', 'Void'));

ALTER TABLE bill
    ADD CONSTRAINT ck_bill_status CHECK (status IN ('Approved', 'Void'));


-- ============================= PART ONE, D4: external_status_raw, VERBATIM
-- C17 requires the raw value stored verbatim on the mirrored row. Before this
-- the only verbatim copy was `integration_inbox.external_status_raw`, so a
-- mirrored document's status could never be reconciled against its source on
-- the row itself.
--
-- VERBATIM means verbatim: not trimmed, not title-cased, not translated, not
-- normalised. NULL is permitted for a locally created record and for a source
-- object that exposes no status at all; nothing is invented to fill it. There
-- is deliberately NO CHECK on these columns -- constraining a raw value is the
-- same mistake as mapping it.
--
-- `integration_inbox`'s copy stays IMMUTABLE INGESTION EVIDENCE. This one
-- exists for direct audit and reporting on the document row, and must agree
-- with its originating inbox record byte for byte.
--
-- `pr_line` is excluded; see the header.
ALTER TABLE purchase_request ADD COLUMN external_status_raw text;
ALTER TABLE purchase_order   ADD COLUMN external_status_raw text;
ALTER TABLE po_line          ADD COLUMN external_status_raw text;
ALTER TABLE grn              ADD COLUMN external_status_raw text;
ALTER TABLE grn_line         ADD COLUMN external_status_raw text;
ALTER TABLE bill             ADD COLUMN external_status_raw text;
ALTER TABLE bill_line        ADD COLUMN external_status_raw text;


-- ========================= PART ONE, D5: ux_grn_line_external was NULLS DISTINCT
-- The most dangerous defect in the set. 013's
-- `ux_grn_line_external UNIQUE (po_line_id, receive_external_id,
-- line_external_id)` uses PostgreSQL's default NULLS DISTINCT, so it does NOT
-- constrain a receive line with no external line id -- which on Zoho ERP is
-- THE ORDINARY CASE, because ERP publishes no receives-list endpoint and lines
-- are discovered PO-anchored.
--
-- The sweeps re-walk by design, on a 300-second overlap with a cycling cursor.
-- So every walk re-inserted, and `received` climbed with no new receive
-- arriving -- identical in shape to the `accumulate_unattributed` adding-bucket
-- defect.
--
-- `line_no` carries the receive line's ordinal so that two GENUINELY DISTINCT
-- lines on one receive stay distinct. It is a new column: 013's `grn_line` has
-- none. Existing rows keep NULL, under which this index is exactly 013's three
-- columns with NULLs grouped -- strictly stronger, never weaker.
ALTER TABLE grn_line ADD COLUMN line_no integer;

ALTER TABLE grn_line DROP CONSTRAINT IF EXISTS ux_grn_line_external;

CREATE UNIQUE INDEX ux_grn_line_external_v2
    ON grn_line (po_line_id, receive_external_id, line_external_id, line_no)
    NULLS NOT DISTINCT;


-- ================== PART ONE, D6: "a PR converts exactly once" is structural
-- `ix_purchase_order_pr` was a PLAIN index, so the rule was enforced only
-- under the purchase request's own row lock in application code. PARTIAL,
-- because a PO need not descend from a PR -- `pr_id` is nullable in both the
-- POC and 013.
DROP INDEX IF EXISTS ix_purchase_order_pr;

CREATE UNIQUE INDEX ux_purchase_order_pr
    ON purchase_order (pr_id)
    WHERE pr_id IS NOT NULL;


-- =============================================== PART TWO, C1: pr_reservation
-- `app/backend/migrations/002_financial_controls.sql:187` defines it and
-- `domain.compute_ledger:171` reads it. Nothing in `migrations/pg/` created it,
-- so `procurement_services.create_pr(reserve=True)` correctly refused with
-- PR_RESERVATION_NOT_MIGRATED rather than writing nothing and reporting
-- success.
--
-- WHY IT MATTERS, IN ONE SENTENCE: without reservations two requestors can each
-- pass `budget_check` against the same rupees, because neither request has
-- taken anything out of availability.
--
-- `integer CHECK (x IN (0,1))` and SQLite TEXT timestamps are dialect
-- workarounds, not intent; the PostgreSQL forms are used. Everything else --
-- the four states, the amount guard, the converted-has-a-PO rule and the one
-- live reservation per PR -- ports 1:1.
CREATE TABLE pr_reservation (
    reservation_id text PRIMARY KEY,
    pr_id          text NOT NULL REFERENCES purchase_request (pr_id),
    wbs_id         text NOT NULL,
    budget_head_id text NOT NULL REFERENCES budget_head (budget_head_id),

    -- Denormalised for `fk_pr_reservation_wbs_project` and for the RLS policy,
    -- exactly as `pr_line.project_id` is. A reservation holds budget on a
    -- control cell; it must not be able to name a WBS element in another
    -- project than the request it belongs to.
    project_id     text NOT NULL,

    -- A reservation of zero or less is not a reservation, and a negative one
    -- would INCREASE availability.
amount_paise   bigint NOT NULL,
    state          text NOT NULL DEFAULT 'Reserved',
    po_id          text REFERENCES purchase_order (po_id),

    settled_at     timestamptz,
    settled_by     text,

    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,

    CONSTRAINT ck_pr_reservation_amount_positive CHECK (amount_paise > 0),
    CONSTRAINT ck_pr_reservation_state CHECK (
        state IN ('Reserved', 'Converted', 'Released', 'Expired')
    ),
    CONSTRAINT ck_pr_reservation_converted_has_po CHECK (
        state <> 'Converted' OR po_id IS NOT NULL
    ),
    -- A settled reservation names WHO and WHEN, or it is not settled -- the
    -- same rule `ck_reconciliation_exception_resolution` states in 011.
    CONSTRAINT ck_pr_reservation_settlement CHECK (
        (state = 'Reserved' AND settled_at IS NULL AND settled_by IS NULL)
        OR (state <> 'Reserved' AND settled_at IS NOT NULL
            AND settled_by IS NOT NULL)
    ),
    CONSTRAINT fk_pr_reservation_wbs_project
        FOREIGN KEY (wbs_id, project_id)
        REFERENCES wbs_element (wbs_id, project_id) ON UPDATE RESTRICT,
    CONSTRAINT fk_pr_reservation_pr_project
        FOREIGN KEY (pr_id, project_id)
        REFERENCES purchase_request (pr_id, project_id) ON UPDATE RESTRICT
);

-- THE WHOLE CONTROL: exactly one live reservation per PR, enforced by a
-- partial unique index rather than by a service remembering to check. Partial
-- on `state = 'Reserved'` deliberately -- once a reservation is converted or
-- released, a later reservation against the same request is a genuinely new
-- one and must be creatable.
--
-- AND IT CONTRADICTS GAP-1, WHICH IS REPORTED RATHER THAN RESOLVED HERE.
--
-- This index is right when a purchase request addresses exactly ONE control
-- cell, which is what the SQLite POC's header-only `purchase_request` did and
-- what AUD-H-001 was written against. `docs/WAVE6_PROCUREMENT_CONTRACT.md`
-- GAP-1 changed the grain: a `pr_line` request may name several
-- `(wbs_id, budget_head_id)` cells, and a reservation holds ONE cell -- so
-- such a request needs one hold per cell, which this index forbids.
--
-- Neither rule may be quietly bent. Widening the index to
-- `(pr_id, wbs_id, budget_head_id)` would drop a control the spec froze and
-- port 1:1; holding the whole sum on the first cell would reserve money
-- against a budget nobody asked to spend. The index is therefore built EXACTLY
-- as specified, and `procurement_services.create_pr` REFUSES a multi-cell
-- `reserve=True` with MULTI_CELL_RESERVATION_UNSUPPORTED, creating nothing.
-- Resolving it is a contract change, not a migration.
CREATE UNIQUE INDEX ux_pr_reservation_live
    ON pr_reservation (pr_id)
    WHERE state = 'Reserved';

CREATE INDEX ix_pr_reservation_cell
    ON pr_reservation (wbs_id, budget_head_id) WHERE state = 'Reserved';
CREATE INDEX ix_pr_reservation_pr ON pr_reservation (pr_id);


-- ================================================ PART TWO, C2: lifecycle_state
-- `domain.lifecycle_permits` (domain.py:287) reads it to answer whether an
-- object's state permits procurement or posting. AUD-C-008. Without the table
-- the gate cannot be evaluated AT ALL, which is why
-- `procurement_services.lifecycle_gate` reported LIFECYCLE_UNAVAILABLE as a
-- SENTENCE -- so a skipped gate could not read as a passed one. This migration
-- makes that sentence unnecessary.
--
-- `integer CHECK (x IN (0,1))` becomes a real `boolean`: the SQLite form is a
-- dialect workaround, not an intent.
--
-- UNKNOWN STATES DENY BY DEFAULT. `lifecycle_permits` returns False for a state
-- absent from this table, and that fail-closed behaviour is preserved exactly
-- -- there is no row here for a state, and no default, that would permit
-- anything the POC did not permit.
CREATE TABLE lifecycle_state (
    object_type        text NOT NULL,
    state              text NOT NULL,
    allows_procurement boolean NOT NULL,
    allows_posting     boolean NOT NULL,
    is_terminal        boolean NOT NULL,
    PRIMARY KEY (object_type, state),
    CONSTRAINT ck_lifecycle_state_object_type CHECK (
        object_type IN ('project', 'wbs')
    )
);

-- Read from `app/backend/migrations/002_financial_controls.sql`'s own
-- `INSERT INTO lifecycle_state` list at implementation time, not retyped from
-- memory. Twenty-one rows: eleven for `project`, ten for `wbs`.
INSERT INTO lifecycle_state
    (object_type, state, allows_procurement, allows_posting, is_terminal)
VALUES
    ('project', 'Draft',                   false, false, false),
    ('project', 'Submitted',               false, false, false),
    ('project', 'Under Review',            false, false, false),
    ('project', 'Approved',                false, false, false),
    ('project', 'Released',                true,  true,  false),
    ('project', 'Technically Completed',   false, true,  false),
    ('project', 'Financially Completed',   false, false, false),
    ('project', 'Awaiting Capitalisation', false, true,  false),
    ('project', 'Capitalised',             false, false, true),
    ('project', 'Closed',                  false, false, true),
    ('project', 'Reopened',                true,  true,  false),
    ('wbs',     'Draft',                   false, false, false),
    ('wbs',     'Submitted',               false, false, false),
    ('wbs',     'Approved',                false, false, false),
    ('wbs',     'Released',                true,  true,  false),
    ('wbs',     'Technically Completed',   false, true,  false),
    ('wbs',     'Financially Completed',   false, false, false),
    ('wbs',     'Awaiting Capitalisation', false, true,  false),
    ('wbs',     'Capitalised',             false, false, true),
    ('wbs',     'Closed',                  false, false, true),
    ('wbs',     'Reopened',                true,  true,  false);


-- ============================== PART TWO, C4: concurrency-safe numbering
-- `procurement_services` reported a PR number from `SELECT COUNT(*) + 1`, which
-- races `ux_purchase_request_number`: two concurrent counts read the same value
-- and both proceed.
--
-- NO NEW MECHANISM IS NEEDED AND NONE IS INVENTED. `numbering_series`,
-- `numbering_counter` and `numbering_issued` already exist
-- (005_master_data.sql:54-104) and `masters.issue_number` already advances the
-- counter with a single atomic `INSERT ... ON CONFLICT ... DO UPDATE ...
-- RETURNING`, whose row lock serialises concurrent issuers. All this migration
-- supplies is the four series.
--
-- `reset_policy = 'YEARLY'` so `period_key` is the fiscal year the caller
-- passes; `numbering_issued` is append-only by 005's triggers, so an issued
-- value is immutable once minted. A gap is acceptable; a reused number is not.
INSERT INTO numbering_series
    (series_id, code, prefix, suffix, pad_width, reset_policy, description,
     created_by, updated_by)
VALUES
    ('NS-PR',   'PURCHASE_REQUEST', 'PR-',   '', 4, 'YEARLY',
     'Purchase requests. period_key is the fiscal year.', 'SYSTEM', 'SYSTEM'),
    ('NS-PO',   'PURCHASE_ORDER',   'PO-',   '', 4, 'YEARLY',
     'Purchase orders. period_key is the fiscal year.', 'SYSTEM', 'SYSTEM'),
    ('NS-GRN',  'GOODS_RECEIPT',    'GRN-',  '', 4, 'YEARLY',
     'Goods receipts raised locally. A mirrored receipt keeps the source number.',
     'SYSTEM', 'SYSTEM'),
    ('NS-BILL', 'VENDOR_BILL',      'BILL-', '', 4, 'YEARLY',
     'Vendor bills raised locally. A mirrored bill keeps the vendor number.',
     'SYSTEM', 'SYSTEM');


-- ================== PART TWO, C5: fractional PO quantity policy, stated
-- `po_line.quantity` is `numeric`; `outbound.PoLine.quantity` is `int`.
-- `procurement_services._emission_lines` refuses a fractional quantity with
-- NON_INTEGER_QUANTITY rather than rounding it.
--
-- THAT REFUSAL IS KEPT AS THE DEFAULT AND MADE CONFIGURABLE, NOT REMOVED.
-- Rounding a quantity silently changes what was ordered; refusing tells
-- somebody. The alternative is explicit and audited: `plan_po_emission`
-- appends an audit event naming every line it rounded and the value it rounded
-- from, so a rounded quantity is never invisible.
--
-- A reference table rather than an environment variable, because this is a
-- business choice a controller makes and a controller must be able to see what
-- it currently is.
CREATE TABLE procurement_policy (
    policy_key   text PRIMARY KEY,
    policy_value text NOT NULL,
    description  text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    version_no   integer NOT NULL DEFAULT 1,
    CONSTRAINT ck_procurement_policy_known CHECK (
        (policy_key = 'FRACTIONAL_PO_QUANTITY'
         AND policy_value IN ('REFUSE', 'ROUND_HALF_UP'))
    )
);

INSERT INTO procurement_policy
    (policy_key, policy_value, description, updated_by)
VALUES
    ('FRACTIONAL_PO_QUANTITY', 'REFUSE',
     'What to do when a po_line.quantity is fractional and the emission DTO '
     'carries an integer count. REFUSE (the working default) raises '
     'NON_INTEGER_QUANTITY and emits nothing. ROUND_HALF_UP rounds and audits '
     'every rounded line, naming the original value.',
     'SYSTEM');


-- ================ PART TWO, C6: procurement lifecycle transitions, as data
-- Plan §12's PR and PO state machines, as rows. An unknown transition is
-- REFUSED rather than permitted, which is the same fail-closed rule
-- `lifecycle_state` follows.
--
-- WHAT CANNOT BE REPRESENTED HERE, AND WHY IT IS NOT SILENTLY ADDED.
--
-- Plan §12 gives the PR machine as
--   DRAFT -> SUBMITTED -> UNDER_REVIEW ->
--     {APPROVED | REJECTED | RETURNED | EXCEPTION_PENDING} -> CONVERTED | CANCELLED
--
-- `Converted` and `Cancelled` are NOT in `ck_purchase_request_status`'s
-- permitted set, which 013 froze as the seven C3 labels the POC writes. A
-- transition row naming a target state the column cannot hold would be a rule
-- about an unreachable state -- worse than absent, because it would read as
-- enforcement. Admitting them would require DROPPING a CHECK 013 created for a
-- reason it stated, which is outside what this migration was approved to do.
--
-- The two transitions are therefore ABSENT and NAMED, here and in the report,
-- rather than invented. Closing them is a deliberate contract change to
-- `ck_purchase_request_status` and to C3, not a schema tidy-up.
CREATE TABLE procurement_transition (
    object_type text NOT NULL,
    from_state  text NOT NULL,
    to_state    text NOT NULL,
    PRIMARY KEY (object_type, from_state, to_state),
    CONSTRAINT ck_procurement_transition_object_type CHECK (
        object_type IN ('purchase_request', 'purchase_order')
    ),
    CONSTRAINT ck_procurement_transition_not_self CHECK (from_state <> to_state)
);

-- Purchase request. Label form, matching `ck_purchase_request_status` (013),
-- not the CODE form plan §12 writes them in -- 013's own header explains at
-- length why the procurement chain is spelled in labels, and a transition table
-- whose states do not match the column it governs governs nothing.
INSERT INTO procurement_transition (object_type, from_state, to_state) VALUES
    ('purchase_request', 'Draft',             'Submitted'),
    ('purchase_request', 'Submitted',         'Under Review'),
    ('purchase_request', 'Submitted',         'Approved'),
    ('purchase_request', 'Submitted',         'Rejected'),
    ('purchase_request', 'Submitted',         'Returned'),
    ('purchase_request', 'Submitted',         'Exception Pending'),
    ('purchase_request', 'Under Review',      'Approved'),
    ('purchase_request', 'Under Review',      'Rejected'),
    ('purchase_request', 'Under Review',      'Returned'),
    ('purchase_request', 'Under Review',      'Exception Pending'),
    ('purchase_request', 'Exception Pending', 'Approved'),
    ('purchase_request', 'Exception Pending', 'Rejected'),
    ('purchase_request', 'Returned',          'Draft'),
    ('purchase_request', 'Returned',          'Submitted');

-- Purchase order. `Cancelled` is reachable from every pre-actualised state
-- (plan §12: "CANCELLED from any pre-actualised state, releasing residual
-- commitment and settling reservations").
INSERT INTO procurement_transition (object_type, from_state, to_state) VALUES
    ('purchase_order', 'Draft',                'Submitted'),
    ('purchase_order', 'Submitted',            'Approved'),
    ('purchase_order', 'Approved',             'Released'),
    ('purchase_order', 'Released',             'Partially Actualised'),
    ('purchase_order', 'Released',             'Fully Committed'),
    ('purchase_order', 'Fully Committed',      'Partially Actualised'),
    ('purchase_order', 'Partially Actualised', 'Fully Actualised'),
    ('purchase_order', 'Fully Actualised',     'Closed'),
    ('purchase_order', 'Released',             'Closed'),
    ('purchase_order', 'Partially Actualised', 'Closed'),
    ('purchase_order', 'Draft',                'Cancelled'),
    ('purchase_order', 'Submitted',            'Cancelled'),
    ('purchase_order', 'Approved',             'Cancelled'),
    ('purchase_order', 'Released',             'Cancelled'),
    ('purchase_order', 'Fully Committed',      'Cancelled');


-- ========================================================= row-level security
-- ENABLE **and** FORCE on all four new tables, plus an entry in BOTH
-- `app/backend/pg/rls.py` and `app/backend/pg/scope_inventory.py`:
-- `tests/test_pg_rls_coverage.py` asserts set equality in both directions, and
-- a table protected here but absent from either registry fails that assertion
-- rather than passing quietly.
--
-- FORCE is the half most easily dropped and the one invisible from
-- `pg_policies`: without it the table's OWNER -- in production the deploy
-- identity that ran the migrations -- bypasses every policy silently.

-- `pr_reservation` reaches all four dimensions through its denormalised
-- `project_id`, which `fk_pr_reservation_pr_project` binds to the request's.
-- The same shape as `pr_line` in 013, and the same reason for passing all four
-- of `project`'s columns rather than the project one alone: dimensions resolve
-- INDEPENDENTLY, so a principal restricted to one entity and to no project
-- carries `project_ids=None`, and a project-only predicate hands them every
-- other entity's reservations. A reservation is held budget; "entity A cannot
-- read entity B's holds" has to hold at the backstop too.
ALTER TABLE pr_reservation ENABLE ROW LEVEL SECURITY;
ALTER TABLE pr_reservation FORCE ROW LEVEL SECURITY;
CREATE POLICY pr_reservation_scope ON pr_reservation
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = pr_reservation.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = pr_reservation.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

-- The three rule tables are ORGANISATION-WIDE REFERENCE DATA: no dimension
-- column and no join to one. "A project in state Released permits procurement"
-- means the same thing in every entity. The only line RLS can draw is between
-- a session with an established principal and one with no scope applied at
-- all, which is exactly `capex_principal_present()` -- the predicate 006 gives
-- `item_master` and `vendor_master`, reused rather than reinvented.
--
-- The alternative -- leaving them unprotected -- is what
-- `test_pg_rls_coverage.py::test_every_table_in_the_schema_is_classified_scoped_or_deliberately_not`
-- exists to make impossible to do by accident.
ALTER TABLE lifecycle_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE lifecycle_state FORCE ROW LEVEL SECURITY;
CREATE POLICY lifecycle_state_scope ON lifecycle_state
    USING (capex_principal_present())
    WITH CHECK (capex_principal_present());

ALTER TABLE procurement_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE procurement_policy FORCE ROW LEVEL SECURITY;
CREATE POLICY procurement_policy_scope ON procurement_policy
    USING (capex_principal_present())
    WITH CHECK (capex_principal_present());

ALTER TABLE procurement_transition ENABLE ROW LEVEL SECURITY;
ALTER TABLE procurement_transition FORCE ROW LEVEL SECURITY;
CREATE POLICY procurement_transition_scope ON procurement_transition
    USING (capex_principal_present())
    WITH CHECK (capex_principal_present());


-- ================================================== privileges for capex_app
-- Stated explicitly rather than relied upon, exactly as 011 and 013 state
-- theirs: 004's ALTER DEFAULT PRIVILEGES attaches to the ROLE THAT ISSUED IT,
-- so a deployment whose 014 is applied by a different identity than its 004
-- would otherwise leave the application unable to read its own new tables.
GRANT SELECT, INSERT, UPDATE ON
    pr_reservation, procurement_policy
    TO capex_app;

-- The three rule tables are READ-ONLY to the application. They are changed by
-- a migration, which is what makes "valid transitions are data, not code"
-- mean something: data the running application can rewrite is code with extra
-- steps.
GRANT SELECT ON lifecycle_state, procurement_transition TO capex_app;

-- ...and the REVOKE that makes "read-only" true rather than merely intended.
-- Granting SELECT alone removes nothing: 004's ALTER DEFAULT PRIVILEGES handed
-- capex_app SELECT, INSERT, UPDATE and DELETE on both tables the instant each
-- came into existence, so without this the application could rewrite the rules
-- it is being gated by -- and a rule table the gated party can edit is not a
-- control, it is a suggestion. DELETE is taken off both below with the others.
REVOKE INSERT, UPDATE ON lifecycle_state, procurement_transition FROM capex_app;

-- ...and DELETE taken away, in the same migration.
--
-- THIS IS NOT BELT-AND-BRACES FOR THE GRANT ABOVE, AND OMITTING DELETE FROM IT
-- ACHIEVES NOTHING. 004's `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT
-- SELECT, INSERT, UPDATE, DELETE ON TABLES TO capex_app` already handed
-- capex_app DELETE on all four the instant each came into existence. This
-- REVOKE is what actually removes it, exactly as 004 does for `audit_log`, 008
-- for `approval_action` and 013 for the eight procurement documents.
--
-- A reservation is RELEASED (`state = 'Released'`), never deleted. That is the
-- whole of AUD-H-001: a reservation is Reserved, then Converted or Released or
-- Expired, EXACTLY ONCE, and a deleted row has no such history.
REVOKE DELETE ON
    pr_reservation, lifecycle_state, procurement_policy, procurement_transition
    FROM capex_app;

-- `procurement_policy` is the one new table an operator changes at runtime, and
-- UPDATE is what that takes. INSERT is granted alongside it only so a row lost
-- to a bad restore can be put back; a NEW policy key still needs a migration,
-- because `ck_procurement_policy_known` names every key and its permitted
-- values and a key outside it is a contract change. Its rows are never deleted
-- -- a policy that stops existing silently reverts every caller to whatever
-- default the code carries, which is the "plausible-looking default" this
-- product refuses everywhere else.

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS.
--   --
--   -- Reverting 014 RE-OPENS the over-commitment hole this wave closed.
--   -- `budget_ledger_cell.actual_paise` and `.pr_reserved_paise` get their
--   -- writers back only because `pr_reservation` exists; drop it and
--   -- `recompute_derived_position` refuses (it does not silently write zero),
--   -- so every cell it touches keeps its last value and availability freezes
--   -- rather than overstating. Loud, and the safe direction, but not free.
--   --
--   -- Dropping the four CHECKs makes `purchase_order.status`, `grn.status` and
--   -- `bill.status` free text again -- and a typo'd 'cancelled' then holds
--   -- commitment forever, silently, which is D3's whole defect.
--   --
--   -- Restoring 013's `ux_grn_number` / `ux_bill_number` will FAIL on any
--   -- database that has since accepted two entities legitimately sharing a
--   -- number. That is not a bug in this block: those rows are the ones D1 was
--   -- written to allow, and there is no correct way to un-allow them except by
--   -- destroying one. Take a dump first, and expect to renumber by hand.
--   --
--   -- No row is deleted anywhere below. The new columns are dropped, which
--   -- discards the values in them (external_status_raw's verbatim copies among
--   -- them) but no document.
--   DROP POLICY IF EXISTS procurement_transition_scope ON procurement_transition;
--   DROP POLICY IF EXISTS procurement_policy_scope ON procurement_policy;
--   DROP POLICY IF EXISTS lifecycle_state_scope ON lifecycle_state;
--   DROP POLICY IF EXISTS pr_reservation_scope ON pr_reservation;
--   DROP TABLE IF EXISTS procurement_transition;
--   DROP TABLE IF EXISTS procurement_policy;
--   DROP TABLE IF EXISTS lifecycle_state;
--   DROP TABLE IF EXISTS pr_reservation;
--   DELETE FROM numbering_series
--    WHERE series_id IN ('NS-PR', 'NS-PO', 'NS-GRN', 'NS-BILL')
--      AND NOT EXISTS (SELECT 1 FROM numbering_issued i
--                       WHERE i.series_id = numbering_series.series_id);
--   DROP INDEX IF EXISTS ux_purchase_order_pr;
--   CREATE INDEX ix_purchase_order_pr
--       ON purchase_order (pr_id) WHERE pr_id IS NOT NULL;
--   DROP INDEX IF EXISTS ux_grn_line_external_v2;
--   ALTER TABLE grn_line
--       ADD CONSTRAINT ux_grn_line_external
--       UNIQUE (po_line_id, receive_external_id, line_external_id);
--   ALTER TABLE grn_line DROP COLUMN IF EXISTS line_no;
--   ALTER TABLE bill_line DROP COLUMN IF EXISTS external_status_raw;
--   ALTER TABLE bill DROP COLUMN IF EXISTS external_status_raw;
--   ALTER TABLE grn_line DROP COLUMN IF EXISTS external_status_raw;
--   ALTER TABLE grn DROP COLUMN IF EXISTS external_status_raw;
--   ALTER TABLE po_line DROP COLUMN IF EXISTS external_status_raw;
--   ALTER TABLE purchase_order DROP COLUMN IF EXISTS external_status_raw;
--   ALTER TABLE purchase_request DROP COLUMN IF EXISTS external_status_raw;
--   ALTER TABLE bill DROP CONSTRAINT IF EXISTS ck_bill_status;
--   ALTER TABLE grn DROP CONSTRAINT IF EXISTS ck_grn_status;
--   ALTER TABLE purchase_order DROP CONSTRAINT IF EXISTS ck_purchase_order_status;
--   DROP INDEX IF EXISTS ux_bill_line_fingerprint;
--   DROP INDEX IF EXISTS ux_bill_line_external;
--   ALTER TABLE bill_line DROP COLUMN IF EXISTS line_fingerprint;
--   ALTER TABLE bill_line DROP COLUMN IF EXISTS line_no;
--   ALTER TABLE bill_line DROP COLUMN IF EXISTS external_line_id;
--   DROP INDEX IF EXISTS ix_bill_vendor;
--   DROP INDEX IF EXISTS ix_bill_entity;
--   DROP INDEX IF EXISTS ix_grn_entity;
--   DROP INDEX IF EXISTS ux_bill_number_scoped;
--   DROP INDEX IF EXISTS ux_grn_number_scoped;
--   DROP INDEX IF EXISTS ux_bill_external_identity;
--   DROP INDEX IF EXISTS ux_grn_external_identity;
--   CREATE UNIQUE INDEX ux_bill_external
--       ON bill (external_source, external_id) WHERE external_id IS NOT NULL;
--   CREATE UNIQUE INDEX ux_grn_external
--       ON grn (external_source, external_id) WHERE external_id IS NOT NULL;
--   ALTER TABLE bill ADD CONSTRAINT ux_bill_number UNIQUE (bill_number);
--   ALTER TABLE grn ADD CONSTRAINT ux_grn_number UNIQUE (grn_number);
--   ALTER TABLE bill DROP COLUMN IF EXISTS vendor_key;
--   ALTER TABLE bill DROP COLUMN IF EXISTS bill_number_normalised;
--   ALTER TABLE bill DROP CONSTRAINT IF EXISTS fk_bill_vendor;
--   ALTER TABLE bill DROP CONSTRAINT IF EXISTS fk_bill_entity;
--   ALTER TABLE bill DROP CONSTRAINT IF EXISTS fk_bill_connection;
--   ALTER TABLE grn DROP CONSTRAINT IF EXISTS fk_grn_numbering_series;
--   ALTER TABLE grn DROP CONSTRAINT IF EXISTS fk_grn_entity;
--   ALTER TABLE grn DROP CONSTRAINT IF EXISTS fk_grn_connection;
--   ALTER TABLE bill DROP COLUMN IF EXISTS vendor_id;
--   ALTER TABLE bill DROP COLUMN IF EXISTS entity_id;
--   ALTER TABLE bill DROP COLUMN IF EXISTS connection_id;
--   ALTER TABLE grn DROP COLUMN IF EXISTS period_key;
--   ALTER TABLE grn DROP COLUMN IF EXISTS numbering_series_id;
--   ALTER TABLE grn DROP COLUMN IF EXISTS entity_id;
--   ALTER TABLE grn DROP COLUMN IF EXISTS connection_id;
--   DELETE FROM schema_migrations WHERE version = '014';
--   COMMIT;
