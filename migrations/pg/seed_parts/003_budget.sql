-- 003_budget.sql
-- Wave 2, stream 1 (Budget Control Backend) seed fragment.
--
-- Loaded after seed_demo.sql (which already populated 001/002's estate --
-- organisation, entities, projects, the WBS hierarchy, budget heads and the
-- Milestone 1 budget_control_cell / budget_ledger_cell rows) and after
-- 003_budget_planning.sql has run. This fragment does three things:
--
--  1. BACKFILLS budget_line provenance for the cells seed_demo.sql already
--     established, so `budget_control_cell.budget_paise` -- now DERIVED from
--     budget_line history (see app/backend/pg/budget.py::recompute_cell) --
--     has real source rows behind it instead of being a value nobody can
--     trace to a decision.
--
--     While doing this it also CORRECTS a pre-existing inconsistency:
--     seed_demo.sql gave WBS-A-CIVIL a budget_ledger_cell.revisions_paise of
--     Rs 5,00,000 (an approved revision) but left budget_control_cell.
--     budget_paise at Rs 80,00,000 -- the original only, never rolled into
--     the control figure. This migration makes budget_line the source of
--     truth budget_paise is computed from, so that gap is closed here by
--     construction: WBS-A-CIVIL's budget_paise becomes Rs 85,00,000 (see
--     the "WBS-A-CIVIL correction" section below).
--
--  2. DEMONSTRATES the full revision/transfer lifecycle: two APPROVED cuts
--     (one lands a cell in WATCH, one in CRITICAL -- required by this
--     stream's brief) plus one still-DRAFT revision and one still-DRAFT
--     transfer, so "a revision creates no spending capacity until approved"
--     is not just an assertion in a test but something visible in the demo
--     data (WBS-A-ELEC's budget_paise stays at its original Rs 40,00,000
--     while its +Rs 2,00,000 revision sits in DRAFT).
--
--  3. Freezes two budget_version snapshots of PRJ-DM-001 (before and after
--     the two approved cuts) for the SCR-10 compare screen to have
--     something real to show.
--
-- Both cuts land on cells that ALREADY demonstrate the "budget owned at two
-- levels of the same chain" shape seed_demo.sql built
-- (WBS-A-CIVIL/WBS-A-CIVIL-FOUND and WBS-A-PM/WBS-A-PM-MILL) -- this
-- fragment reinforces that shape with budget_line provenance rather than
-- inventing a new one.
--
-- All money is integer PAISE (bigint). Every literal is annotated with its
-- rupee value in a comment; none is ever written as a decimal.

-- ================================================================ ORIGINAL
-- provenance for every cell seed_demo.sql already gave a non-zero
-- original_paise. effective_from mirrors the project's own created_at date
-- (or, where seed_demo.sql's audit log names a specific approval date, that
-- date) -- all comfortably in the past relative to "today", so every row
-- below lands in the CURRENT bucket, not FUTURE.
INSERT INTO budget_line
    (budget_line_id, wbs_id, budget_head_id, kind, amount_paise, effective_from,
     status, justification, created_by, updated_by)
VALUES
    ('BL-DM1-CIVIL-ORIG', 'WBS-A-CIVIL', 'BH-DM1-CIVIL', 'ORIGINAL',
     800000000, '2026-04-05', 'Approved',                    -- Rs 80,00,000
     'Original CAPEX budget for civil and structural work.', 'U-PFC', 'U-PFC'),
    ('BL-DM1-CIVIL-REV1', 'WBS-A-CIVIL', 'BH-DM1-CIVIL', 'REVISION',
     50000000, '2026-06-15', 'Approved',                      -- +Rs 5,00,000
     'Additional scope: retaining wall extension.', 'U-CFO', 'U-CFO'),
    ('BL-DM1-CIVILFOUND-ORIG', 'WBS-A-CIVIL-FOUND', 'BH-DM1-CIVIL', 'ORIGINAL',
     200000000, '2026-04-05', 'Approved',                     -- Rs 20,00,000
     'Original budget for foundation and piling.', 'U-PFC', 'U-PFC'),
    ('BL-DM1-PM-ORIG', 'WBS-A-PM', 'BH-DM1-PM', 'ORIGINAL',
     1200000000, '2026-04-05', 'Approved',                    -- Rs 1,20,00,000
     'Original CAPEX budget for plant and machinery.', 'U-PFC', 'U-PFC'),
    -- Effective in a FUTURE period (P-DM1-Q3 opens 2026-10-01): demonstrates
    -- future_budget_paise being populated from real budget_line history
    -- rather than a hand-set ledger column.
    ('BL-DM1-PM-FUTURE', 'WBS-A-PM', 'BH-DM1-PM', 'ORIGINAL',
     100000000, '2026-10-01', 'Approved',                     -- Rs 10,00,000
     'Phase 2 plant expansion, approved ahead of its effective quarter.',
     'U-PFC', 'U-PFC'),
    ('BL-DM1-PMMILL-ORIG', 'WBS-A-PM-MILL', 'BH-DM1-PM', 'ORIGINAL',
     300000000, '2026-04-05', 'Approved',                     -- Rs 30,00,000
     'Original budget for rolling mill equipment.', 'U-PFC', 'U-PFC'),
    ('BL-DM1-ELEC-ORIG', 'WBS-A-ELEC', 'BH-DM1-ELEC', 'ORIGINAL',
     400000000, '2026-04-05', 'Approved',                     -- Rs 40,00,000
     'Original budget for electrical installations.', 'U-PFC', 'U-PFC'),
    ('BL-DM2-PM-ORIG', 'WBS-B-PM', 'BH-DM2-PM', 'ORIGINAL',
     900000000, '2026-04-14', 'Approved',                     -- Rs 90,00,000
     'Original CAPEX budget for solar modules and inverters.', 'U-PFC', 'U-PFC'),
    ('BL-DM2-PMMOD-ORIG', 'WBS-B-PM-MOD', 'BH-DM2-PM', 'ORIGINAL',
     250000000, '2026-04-14', 'Approved',                     -- Rs 25,00,000
     'Original budget for module installation.', 'U-PFC', 'U-PFC'),
    ('BL-DM2-CIVIL-ORIG', 'WBS-B-CIVIL', 'BH-DM2-CIVIL', 'ORIGINAL',
     300000000, '2026-04-14', 'Approved',                     -- Rs 30,00,000
     'Original budget for balance-of-plant civil works.', 'U-PFC', 'U-PFC');

-- =============================================== two APPROVED revisions
-- Both are cuts (negative delta) reallocating money elsewhere, and both are
-- sized to demonstrably trip WATCH and CRITICAL respectively once the
-- corresponding budget_control_cell.budget_paise is updated below.
--
--   WBS-A-CIVIL-FOUND: subtree exposure (FOUND + FOUND-PIL + FOUND-CONC) is
--     fixed by seed_demo.sql at Rs 11,00,000 (commitment+actual+pr_reserved
--     = 40+20+10 lakh on PIL, 0+40+0 lakh on CONC). Cutting the cell's own
--     budget from Rs 20,00,000 to Rs 12,00,000 (-Rs 8,00,000) brings
--     utilisation to 11/12 = 91.7% -> CRITICAL (>= domain.CRITICAL_PCT, 90%).
--
--   WBS-A-PM-MILL: subtree exposure (MILL + MILL-INSTALL) is fixed at
--     Rs 17,00,000 (commitment+actual+pr_reserved = 100+50+20 lakh on
--     MILL-INSTALL). Cutting the cell's own budget from Rs 30,00,000 to
--     Rs 20,00,000 (-Rs 10,00,000) brings utilisation to 17/20 = 85% ->
--     WATCH (>= domain.WATCH_PCT, 80%, and < CRITICAL_PCT).
-- budget_line FIRST: budget_revision.budget_line_id carries a foreign key
-- to it, so the reverse order -- which is how this shipped -- fails on that
-- constraint the moment the fragment actually loads. It never did load
-- until the loader itself was fixed, which is why nothing caught it.
INSERT INTO budget_line
    (budget_line_id, wbs_id, budget_head_id, kind, amount_paise, effective_from,
     status, justification, created_by, updated_by)
VALUES
    ('BL-DM1-CIVILFOUND-CUT', 'WBS-A-CIVIL-FOUND', 'BH-DM1-CIVIL', 'REVISION',
     -80000000, '2026-07-10', 'Approved',
     'Reallocating foundation contingency to Plant & Machinery acceleration.',
     'U-CFO', 'U-CFO'),
    ('BL-DM1-PMMILL-CUT', 'WBS-A-PM-MILL', 'BH-DM1-PM', 'REVISION',
     -100000000, '2026-07-10', 'Approved',
     'Reallocating from mill equipment contingency to the solar park (PRJ-DM-002).',
     'U-CFO', 'U-CFO');

INSERT INTO budget_revision
    (revision_id, wbs_id, budget_head_id, delta_paise, effective_from,
     justification, status, budget_line_id, created_by, decided_at, decided_by)
VALUES
    ('REV-DM1-CIVILFOUND-CUT', 'WBS-A-CIVIL-FOUND', 'BH-DM1-CIVIL',
     -80000000, '2026-07-10',                                  -- -Rs 8,00,000
     'Reallocating foundation contingency to Plant & Machinery acceleration.',
     'APPROVED', 'BL-DM1-CIVILFOUND-CUT', 'U-PM',
     '2026-07-10T09:00:00+00:00'::timestamptz, 'U-CFO'),
    ('REV-DM1-PMMILL-CUT', 'WBS-A-PM-MILL', 'BH-DM1-PM',
     -100000000, '2026-07-10',                                 -- -Rs 10,00,000
     'Reallocating from mill equipment contingency to the solar park (PRJ-DM-002).',
     'APPROVED', 'BL-DM1-PMMILL-CUT', 'U-PM',
     '2026-07-10T09:15:00+00:00'::timestamptz, 'U-CFO');

-- ---- apply the two approved cuts and the WBS-A-CIVIL correction (see the
-- file header) to the Milestone 1 cells this fragment is not otherwise
-- allowed to edit the DEFINITION of, only the DATA of.
UPDATE budget_control_cell SET budget_paise = 850000000, updated_by = 'U-CFO'  -- Rs 85,00,000
    WHERE wbs_id = 'WBS-A-CIVIL' AND budget_head_id = 'BH-DM1-CIVIL';
UPDATE budget_ledger_cell SET revisions_paise = 50000000, updated_by = 'U-CFO' -- unchanged value,
    WHERE wbs_id = 'WBS-A-CIVIL' AND budget_head_id = 'BH-DM1-CIVIL';          -- now traceable

UPDATE budget_control_cell SET budget_paise = 120000000, updated_by = 'U-CFO'  -- Rs 12,00,000
    WHERE wbs_id = 'WBS-A-CIVIL-FOUND' AND budget_head_id = 'BH-DM1-CIVIL';
UPDATE budget_ledger_cell SET revisions_paise = -80000000, updated_by = 'U-CFO'
    WHERE wbs_id = 'WBS-A-CIVIL-FOUND' AND budget_head_id = 'BH-DM1-CIVIL';

UPDATE budget_control_cell SET budget_paise = 200000000, updated_by = 'U-CFO'  -- Rs 20,00,000
    WHERE wbs_id = 'WBS-A-PM-MILL' AND budget_head_id = 'BH-DM1-PM';
UPDATE budget_ledger_cell SET revisions_paise = -100000000, updated_by = 'U-CFO'
    WHERE wbs_id = 'WBS-A-PM-MILL' AND budget_head_id = 'BH-DM1-PM';

UPDATE budget_control_cell SET budget_paise = 1200000000, updated_by = 'U-PFC' -- unchanged;
    WHERE wbs_id = 'WBS-A-PM' AND budget_head_id = 'BH-DM1-PM';                -- the +10L grant
UPDATE budget_ledger_cell SET future_budget_paise = 100000000, updated_by = 'U-PFC' -- is FUTURE
    WHERE wbs_id = 'WBS-A-PM' AND budget_head_id = 'BH-DM1-PM';                -- (see BL-DM1-PM-FUTURE)

-- =============================================== one DRAFT revision
-- Demonstrates "a revision creates no spending capacity until approved":
-- WBS-A-ELEC's budget_control_cell.budget_paise stays at its original
-- Rs 40,00,000 (untouched below) while this sits in DRAFT.
INSERT INTO budget_revision
    (revision_id, wbs_id, budget_head_id, delta_paise, effective_from, justification,
     status, created_by)
VALUES
    ('REV-DM1-ELEC-INC', 'WBS-A-ELEC', 'BH-DM1-ELEC', 20000000, '2026-09-01',
     'Additional switchgear for the expanded electrical scope; pending Finance sign-off.',
     'DRAFT', 'U-PM');

-- =============================================== one DRAFT transfer
-- Moves Rs 2,00,000 of PM budget from PRJ-DM-002's module cell to its civil
-- works cell (a cross-head transfer within the same project). Neither cell's
-- budget_control_cell.budget_paise is touched while this sits in DRAFT.
INSERT INTO budget_transfer
    (transfer_id, from_wbs_id, from_head_id, to_wbs_id, to_head_id, amount_paise,
     effective_from, justification, status, created_by)
VALUES
    ('TRF-DM2-PMMOD-TO-CIVIL', 'WBS-B-PM-MOD', 'BH-DM2-PM', 'WBS-B-CIVIL', 'BH-DM2-CIVIL',
     20000000, '2026-09-01',                                    -- Rs 2,00,000
     'Civil works scope growth on the inverter yard access road; funded from module '
     'installation contingency.', 'DRAFT', 'U-PM');

-- =============================================== SCR-10: two version snapshots
-- v1 "Original approved": every PRJ-DM-001 cell's own budget_paise exactly
-- as seed_demo.sql first established it -- i.e. BEFORE the WBS-A-CIVIL
-- correction and the two cuts above.
-- v2 "Post Wave-2 revisions": the same cells' budget_paise as they stand
-- after this fragment. Comparing v1 to v2 shows exactly the three cells
-- this fragment changed (WBS-A-CIVIL, WBS-A-CIVIL-FOUND, WBS-A-PM-MILL) with
-- a zero delta everywhere else -- real data for GET /api/budget/compare.
INSERT INTO budget_version (version_id, project_id, version_no, label, created_by)
VALUES
    ('BV-DM1-001-BASELINE', 'PRJ-DM-001', 1, 'Original approved', 'U-PFC'),
    ('BV-DM1-001-CURRENT', 'PRJ-DM-001', 2, 'Post Wave-2 revisions', 'U-CFO');

INSERT INTO budget_version_cell (version_id, wbs_id, budget_head_id, budget_paise)
VALUES
    ('BV-DM1-001-BASELINE', 'WBS-A-CIVIL',            'BH-DM1-CIVIL', 800000000),
    ('BV-DM1-001-BASELINE', 'WBS-A-CIVIL-FOUND',      'BH-DM1-CIVIL', 200000000),
    ('BV-DM1-001-BASELINE', 'WBS-A-CIVIL-FOUND-PIL',  'BH-DM1-CIVIL', 0),
    ('BV-DM1-001-BASELINE', 'WBS-A-CIVIL-FOUND-CONC', 'BH-DM1-CIVIL', 0),
    ('BV-DM1-001-BASELINE', 'WBS-A-CIVIL-STRUCT',     'BH-DM1-CIVIL', 0),
    ('BV-DM1-001-BASELINE', 'WBS-A-PM',               'BH-DM1-PM',    1200000000),
    ('BV-DM1-001-BASELINE', 'WBS-A-PM-MILL',          'BH-DM1-PM',    300000000),
    ('BV-DM1-001-BASELINE', 'WBS-A-PM-MILL-INSTALL',  'BH-DM1-PM',    0),
    ('BV-DM1-001-BASELINE', 'WBS-A-ELEC',             'BH-DM1-ELEC',  400000000),
    ('BV-DM1-001-CURRENT',  'WBS-A-CIVIL',            'BH-DM1-CIVIL', 850000000),
    ('BV-DM1-001-CURRENT',  'WBS-A-CIVIL-FOUND',      'BH-DM1-CIVIL', 120000000),
    ('BV-DM1-001-CURRENT',  'WBS-A-CIVIL-FOUND-PIL',  'BH-DM1-CIVIL', 0),
    ('BV-DM1-001-CURRENT',  'WBS-A-CIVIL-FOUND-CONC', 'BH-DM1-CIVIL', 0),
    ('BV-DM1-001-CURRENT',  'WBS-A-CIVIL-STRUCT',     'BH-DM1-CIVIL', 0),
    ('BV-DM1-001-CURRENT',  'WBS-A-PM',               'BH-DM1-PM',    1200000000),
    ('BV-DM1-001-CURRENT',  'WBS-A-PM-MILL',          'BH-DM1-PM',    200000000),
    ('BV-DM1-001-CURRENT',  'WBS-A-PM-MILL-INSTALL',  'BH-DM1-PM',    0),
    ('BV-DM1-001-CURRENT',  'WBS-A-ELEC',             'BH-DM1-ELEC',  400000000);

-- =============================================== audit trail continuation
-- Continues the PROJECT:PRJ-DM-001 hash chain seed_demo.sql started (last
-- entry there is seq 4), with the exact algorithm
-- app.backend.pg.audit.compute_entry_hash uses. See seed_demo.sql's own
-- audit section for the pattern this mirrors.
WITH prev AS (
    SELECT entry_hash FROM audit_log WHERE stream_key = 'PROJECT:PRJ-DM-001' AND seq = 4
)
INSERT INTO audit_log
    (stream_key, seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash)
SELECT
    'PROJECT:PRJ-DM-001', 5, '2026-07-10T09:00:00+00:00'::timestamptz,
    'U-CFO', 'REVISION_APPROVE', 'BUDGET_CELL', 'WBS-A-CIVIL-FOUND:BH-DM1-CIVIL',
    'Revision approved: -INR 8,00,000 (reallocated to Plant & Machinery acceleration)',
    prev.entry_hash,
    encode(digest(
        prev.entry_hash || '|2026-07-10T09:00:00+00:00|U-CFO|REVISION_APPROVE|BUDGET_CELL|' ||
        'WBS-A-CIVIL-FOUND:BH-DM1-CIVIL|Revision approved: -INR 8,00,000 ' ||
        '(reallocated to Plant & Machinery acceleration)', 'sha256'), 'hex')
FROM prev;

WITH prev AS (
    SELECT entry_hash FROM audit_log WHERE stream_key = 'PROJECT:PRJ-DM-001' AND seq = 5
)
INSERT INTO audit_log
    (stream_key, seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash)
SELECT
    'PROJECT:PRJ-DM-001', 6, '2026-07-10T09:15:00+00:00'::timestamptz,
    'U-CFO', 'REVISION_APPROVE', 'BUDGET_CELL', 'WBS-A-PM-MILL:BH-DM1-PM',
    'Revision approved: -INR 10,00,000 (reallocated from mill equipment contingency)',
    prev.entry_hash,
    encode(digest(
        prev.entry_hash || '|2026-07-10T09:15:00+00:00|U-CFO|REVISION_APPROVE|BUDGET_CELL|' ||
        'WBS-A-PM-MILL:BH-DM1-PM|Revision approved: -INR 10,00,000 ' ||
        '(reallocated from mill equipment contingency)', 'sha256'), 'hex')
FROM prev;
