-- 015_reservation_grain.sql
-- One live reservation per PURCHASE REQUEST becomes one per (purchase request x resolved budget control cell).
--
-- WHAT THIS CHANGES, AND WHY 014 IS NOT EDITED
--
-- `014_procurement_corrections.sql` built
--
--     ux_pr_reservation_live  UNIQUE (pr_id) WHERE state = 'Reserved'
--
-- and its own header says, at length, that the index is EXACTLY RIGHT for a
-- purchase request that addresses one control cell and EXACTLY WRONG for a
-- `pr_line`-grained one. It reported that contradiction (GAP-1) rather than
-- resolving it on its own authority, and `procurement_services.create_pr`
-- refused a multi-cell `reserve=True` with MULTI_CELL_RESERVATION_UNSUPPORTED
-- -- safe, and a blocked feature.
--
-- The product owner resolved it on 2026-09-08. THE APPROVED GRAIN IS ONE LIVE
-- RESERVATION PER (PURCHASE REQUEST x RESOLVED BUDGET CONTROL CELL). This
-- migration is that decision, and nothing else.
--
-- 014 IS NOT REWRITTEN. Its checksum is recorded in `schema_migrations` on
-- every database where it ran; editing it makes `assert_schema_current` report
-- drift and every existing deployment refuse to boot. That is the same reason
-- 014 exists rather than 013 having been corrected in place, and it applies
-- here unchanged. 015 is ADDITIVE: it adds one column, one named CHECK, two
-- named indexes, and drops exactly one index -- the one the decision replaces.
--
--
-- WHAT A RESOLVED CONTROL CELL IS, AND WHY IT IS NOT THE LINE'S OWN WBS
--
-- Budget is owned by an ANCESTOR. `budget.check_availability` resolves the
-- nearest ancestor-or-self of a spend whose own `budget_paise` is non-zero for
-- the head (`budget._owning_ancestor`), and availability is a property of that
-- owner's whole subtree. A resolved control cell is therefore
--
--     (the budget-owning WBS ancestor of the line's wbs_id for this head,
--      budget_head_id)
--
-- and NOT `(pr_line.wbs_id, budget_head_id)`. The distinction is the whole
-- point of the new grain:
--
--   * Two lines on DIFFERENT WBS elements that roll up to the SAME owner for
--     the same head are competing for ONE pot. They must aggregate into ONE
--     reservation, or two halves each pass a check against the same rupees and
--     the pair overspends -- which is exactly the defect
--     `procurement_services.budget_verdicts` already aggregates by owning cell
--     to prevent, and a reservation grain that did not match it would re-open
--     it one layer down.
--   * Two lines resolving to DIFFERENT owners are competing for DIFFERENT
--     pots, and need one hold EACH. That is the case 014's index forbade.
--
-- Indexing on the line's own cell would have satisfied the letter of "one hold
-- per cell" and permitted two holds against one pot. The uniqueness below is
-- over the cell the money actually comes out of.
--
--
-- THE INDEX, NAMED, PARTIAL, AND WHY BOTH
--
--     ux_pr_reservation_live_cell
--         UNIQUE (pr_id, wbs_id, budget_head_id) WHERE state = 'Reserved'
--
-- NAMED, because `migrate_pg._indexes_created_by` and
-- `migrate_pg._named_constraints_by` can only verify an object that has a name
-- to look up; an anonymous one certifies as adopted while absent. PARTIAL on
-- `state = 'Reserved'` for 014's reason, unchanged: once a reservation is
-- Converted, Released or Expired, a later hold on the same request and the
-- same cell is a genuinely NEW reservation and must be creatable. The whole of
-- AUD-H-001 -- Reserved, then Converted or Released or Expired, EXACTLY ONCE
-- -- survives this change; only the key it is enforced per changes.
--
-- The new index is strictly WEAKER than the one it replaces: every set of rows
-- satisfying UNIQUE (pr_id) also satisfies UNIQUE (pr_id, wbs_id,
-- budget_head_id). Nothing that was refused before and should still be refused
-- becomes permitted, EXCEPT the one thing the decision permits: a second live
-- hold on the same request against a DIFFERENT cell. The service is what stops
-- that second hold being a duplicate of the first against the same pot, and it
-- does so by resolving to the owner before it writes -- see
-- `procurement_services.reserve_pr_cells`.
--
--
-- cell_grain: THE ONE THING A WEAKER INDEX CANNOT SAY BY ITSELF
--
-- Rows written before this migration hold `wbs_id` = THE LINE'S OWN CELL,
-- because that is what `create_reservation` was given. Rows written after it
-- hold `wbs_id` = THE RESOLVED OWNER. Both are legitimate, both are preserved,
-- and the two are indistinguishable from the row alone -- which matters,
-- because a live LINE-grain hold at a descendant and a live RESOLVED-grain
-- hold at its owner are TWO ROWS AT TWO DIFFERENT KEYS holding THE SAME MONEY
-- TWICE. The new index cannot see that; it is not a duplicate key.
--
-- `cell_grain` records which of the two a row is, so the service can refuse
-- the overlap rather than compute it wrongly, and so an operator reading the
-- table knows what `wbs_id` means on any given row. It DEFAULTS to 'LINE',
-- which is not a guess: every row that exists when this migration runs was
-- written at line grain by construction, because the resolved grain did not
-- exist until this statement.
--
--
-- EVERY HISTORICAL ROW IS PRESERVED
--
-- No DELETE, no TRUNCATE, no DROP TABLE, no merge, no renumbering, no
-- "pick the newest". Every Released, Converted and Expired reservation stays
-- exactly as it is, with its `settled_at`, its `settled_by` and its `po_id`
-- intact -- that history IS AUD-H-001's evidence, and a migration that tidied
-- it away would delete the proof of the control while claiming to strengthen
-- it. The only row-level write below is the DEFAULT that populates a column
-- that did not exist a statement ago.
--
--
-- THE PREFLIGHT REFUSES; IT NEVER RESOLVES
--
-- 014's rule for its own preflight, applied here: find every row that would
-- prevent the new index being built, NAME it, and then RAISE. It never
-- deletes, merges or chooses between financial documents. In practice the
-- check should find nothing -- the old index made a duplicate
-- (pr_id, wbs_id, budget_head_id) live pair impossible, since it made a
-- duplicate `pr_id` live pair impossible -- and it runs anyway, because
-- "should be impossible" is a belief about a database this migration has never
-- seen. A hand-applied schema, a restore that lost the index, or a
-- `schema_migrations` row lost after 014 all produce a database where the
-- belief is false, and the loud direction is the safe one.
--
-- It also reports, WITHOUT refusing, every live reservation whose `wbs_id` is
-- not the budget-owning ancestor for its head. Those rows do not block the
-- index and are not defects: they are correct 014-grain holds, and refusing
-- them would refuse to migrate a database that is doing nothing wrong. They
-- are reported because the service will refuse to add a resolved-grain hold to
-- a request that still carries one, and an operator is better told that here
-- than by a 409 later.
--
--
-- MONEY
--
-- 015 adds NO `*_paise` column. The rule is recorded anyway because it is the
-- rule the NEXT migration to touch this table has to obey:
-- `migrate_pg._PAISE_COLUMN_RE` is anchored at `^` against each stripped field
-- of a `CREATE TABLE` body, so a paise column that does not START its
-- declaration line is a money column whose bigint-ness never verifies, and
-- drift to `numeric(18,2)` then certifies as adopted.
-- `pr_reservation.amount_paise` is untouched here and remains `bigint`.
-- `SUM()` over `bigint` returns `numeric` in PostgreSQL, so every aggregate
-- over it casts `::bigint`; the preflight below does that and so does
-- `procurement_services._RECOMPUTE_DERIVED_SQL`.
--
--
-- RLS, GRANTS AND WHAT IS DELIBERATELY NOT RESTATED
--
-- 014 enabled and forced row-level security on `pr_reservation` and created
-- `pr_reservation_scope`. 015 adds no table, so it adds no policy: a second
-- CREATE POLICY on the same table would ADD a permissive alternative, which
-- WIDENS access rather than confirming it. The privileges ARE restated, as 011
-- and 014 state theirs, because 004's ALTER DEFAULT PRIVILEGES attaches to the
-- role that issued it and a deployment whose 015 is applied by a different
-- identity must not be left guessing. DELETE is revoked again for the same
-- reason it was revoked in 014 -- a released reservation stops holding budget
-- because `state <> 'Reserved'`, never because it stopped existing.

BEGIN;

-- 014's tables are FORCE RLS and the migration runs as the table owner, so a
-- migration identity that is not a superuser sees an EMPTY `pr_reservation`
-- and a preflight that inspects nothing reports clean. Transaction-scoped;
-- expires at the COMMIT below. Same statement, same reason, as 014.
SET LOCAL capex.read_all = 'true';

-- ============================================================== PREFLIGHT
-- Runs BEFORE any DDL. Names every offending row, then refuses.
DO $preflight$
DECLARE
    offending  record;
    problems   integer := 0;
    legacy     integer := 0;
    inspected  integer := 0;
BEGIN
    RAISE NOTICE '015 preflight: checking existing reservations against ux_pr_reservation_live_cell.';

    -- THE BLOCKER. Two or more LIVE reservations already sharing
    -- (pr_id, wbs_id, budget_head_id). The new unique index cannot be built
    -- over them, and nothing here decides which of them is the real hold.
    FOR offending IN
        SELECT r.pr_id, r.wbs_id, r.budget_head_id,
               count(*) AS n,
               COALESCE(SUM(r.amount_paise), 0)::bigint AS held_paise,
               string_agg(r.reservation_id, ', '
                          ORDER BY r.reservation_id) AS ids
        FROM pr_reservation r
        WHERE r.state = 'Reserved'
        GROUP BY r.pr_id, r.wbs_id, r.budget_head_id
        HAVING count(*) > 1
        ORDER BY 1, 2, 3
    LOOP
        problems := problems + 1;
        RAISE NOTICE 'PREFLIGHT ux_pr_reservation_live_cell: purchase request % already carries % LIVE reservations on cell (%, %), holding % paise between them (reservation_id: %). ux_pr_reservation_live should have made this impossible, so this database did not get it. Settle all but one -- state Released or Expired, with settled_at and settled_by -- before applying 015. This migration does not choose which hold is real.',
            offending.pr_id, offending.n, offending.wbs_id,
            offending.budget_head_id, offending.held_paise, offending.ids;
    END LOOP;

    IF problems > 0 THEN
        RAISE EXCEPTION
            '015 preflight refused: % duplicate live reservation group(s) reported above. NOTHING has been changed. Fix the named rows and re-run. This migration does not delete, merge or choose between held budgets.',
            problems
            USING ERRCODE = 'raise_exception';
    END IF;

    -- NOT A BLOCKER, AND REPORTED ANYWAY. A live 014-grain hold sits on the
    -- line's own cell; the resolved grain sits on the budget-owning ancestor.
    -- Both are legitimate and both are kept. The service refuses to add a
    -- resolved hold to a request that still carries a line-grain one, because
    -- the two would hold the same money twice at two different keys, so an
    -- operator is told here rather than by a 409 later.
    FOR offending IN
        SELECT r.pr_id, r.reservation_id, r.wbs_id, r.budget_head_id,
               r.amount_paise
        FROM pr_reservation r
        WHERE r.state = 'Reserved'
          AND NOT EXISTS (
              SELECT 1
              FROM budget_control_cell bc
              WHERE bc.wbs_id = r.wbs_id
                AND bc.budget_head_id = r.budget_head_id
                AND bc.budget_paise <> 0)
        ORDER BY 1, 2
    LOOP
        legacy := legacy + 1;
        RAISE NOTICE 'PREFLIGHT note (not a refusal): live reservation % on request % holds % paise on (%, %), which owns no budget for that head -- it is a 014-grain hold on a descendant of the owning cell. It is PRESERVED unchanged. A resolved-grain hold cannot be added to this request until it is settled, or the same money would be held twice.',
            offending.reservation_id, offending.pr_id, offending.amount_paise,
            offending.wbs_id, offending.budget_head_id;
    END LOOP;

    -- Not a "0 problems" line only: an operator needs to see the checks ran at
    -- all against a database whose reservation table is empty.
    SELECT count(*) INTO inspected FROM pr_reservation;
    RAISE NOTICE '015 preflight: clean. % reservation(s) inspected, % live row(s) noted as 014-grain.',
        inspected, legacy;
END
$preflight$;


-- ====================================================== THE GRAIN, RECORDED
-- DEFAULT 'LINE' is not a guess. Every row in existence at this point was
-- written by `procurement_services.create_reservation` from a cell its caller
-- supplied, which is the line's own cell; the resolved grain did not exist
-- until the statements below. NOT NULL with a default so no historical row is
-- left in a third, unstated condition -- "we do not know what this row's
-- wbs_id means" is precisely the answer this column exists to abolish.
ALTER TABLE pr_reservation ADD COLUMN cell_grain text NOT NULL DEFAULT 'LINE';

-- Named, because `migrate_pg._added_constraints_by` reports only
-- `ADD CONSTRAINT name` and an anonymous CHECK has no `pg_constraint.conname`
-- to look up -- it would certify as adopted while absent. Two values and no
-- more: a third would be a grain nothing resolves, and the point of the column
-- is that `wbs_id` means exactly one of two things.
ALTER TABLE pr_reservation
    ADD CONSTRAINT ck_pr_reservation_cell_grain
    CHECK (cell_grain IN ('LINE', 'RESOLVED'));


-- ================================================== THE REPLACEMENT INDEX
-- Built BEFORE the old one is dropped. Inside one transaction the ordering
-- cannot be observed, and it is written this way regardless: there is no point
-- in the file at which this table is under no live-reservation uniqueness rule
-- at all, and a reader should not have to reason about whether there is.
CREATE UNIQUE INDEX ux_pr_reservation_live_cell
    ON pr_reservation (pr_id, wbs_id, budget_head_id)
    WHERE state = 'Reserved';

-- The rule the decision of 2026-09-08 replaces. Dropped, not left alongside:
-- leaving it would keep refusing the second cell's hold and the feature would
-- still be blocked, with two indexes disagreeing about the grain and the
-- narrower one silently winning.
DROP INDEX ux_pr_reservation_live;

-- `settle_reservations` and the idempotent re-reserve both ask "which live
-- reservations does this request have?" on every conversion, release and
-- retry. `ix_pr_reservation_pr` (014) is unpartitioned and answers it by
-- reading every settled row of the request's history too, which is exactly the
-- set that grows without bound.
CREATE INDEX ix_pr_reservation_live_by_pr
    ON pr_reservation (pr_id, wbs_id, budget_head_id)
    WHERE state = 'Reserved';


-- ================================================== privileges for capex_app
-- Restated rather than relied upon, exactly as 011 and 014 state theirs: 004's
-- ALTER DEFAULT PRIVILEGES attaches to the ROLE THAT ISSUED IT, so a
-- deployment whose 015 is applied by a different identity than its 004 would
-- otherwise be left guessing. Nothing here widens anything -- these are 014's
-- privileges, named again against the table 015 has just changed the shape of.
GRANT SELECT, INSERT, UPDATE ON pr_reservation TO capex_app;

-- And the REVOKE that makes the rest true. A reservation is RELEASED
-- (`state = 'Released'`), never deleted -- AUD-H-001 is "Reserved, then
-- Converted or Released or Expired, EXACTLY ONCE", and a deleted row has no
-- such history. Restated here because this migration's whole subject is which
-- rows may exist live, and a DELETE privilege is the one way to make that
-- question meaningless.
REVOKE DELETE ON pr_reservation FROM capex_app;

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- Reverting restores 014's grain, and with it 014's contradiction: a
--   -- multi-cell `reserve=True` becomes unsatisfiable again. The CREATE below
--   -- FAILS, loudly and correctly, if any purchase request currently holds
--   -- more than one live reservation -- which is the state this migration
--   -- exists to permit. Settle the extra holds first (Released or Expired,
--   -- with settled_at and settled_by); do NOT delete them.
--   DROP INDEX IF EXISTS ix_pr_reservation_live_by_pr;
--   CREATE UNIQUE INDEX ux_pr_reservation_live
--       ON pr_reservation (pr_id)
--       WHERE state = 'Reserved';
--   DROP INDEX IF EXISTS ux_pr_reservation_live_cell;
--   ALTER TABLE pr_reservation
--       DROP CONSTRAINT IF EXISTS ck_pr_reservation_cell_grain;
--   ALTER TABLE pr_reservation DROP COLUMN IF EXISTS cell_grain;
--   -- No row is deleted here and none should be. Every reservation this
--   -- migration's grain permitted stays exactly where it is.
--   COMMIT;
