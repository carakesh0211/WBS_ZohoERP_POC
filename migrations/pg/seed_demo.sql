-- seed_demo.sql
-- Demonstration CAPEX estate for the PostgreSQL foundation
-- (001_foundation.sql, 002_budget_control.sql).
--
-- NOT a numbered migration: migrate_pg.discover() only picks up
-- `NNN_lower_snake.sql`, so this file is never applied by `--upgrade`. It is
-- loaded only by `migrate_pg.fresh(..., seed=True)` (disposable databases
-- only) or by `app.backend.pg.seed.seed()`, which gates it behind an
-- explicit demo profile, a disposable database name, AND an emptiness check
-- -- see that module for the guard. Never run this by hand against a
-- database you have not verified is disposable.
--
-- All company, plant and person names below are invented for this
-- demonstration. They do not name any real Fable5/RAPGURU client, and the
-- statutory identity columns (gst_no, pan_no) are left NULL rather than
-- populated with plausible-looking fake values.
--
-- All money is integer PAISE (bigint). Every literal below is annotated with
-- its rupee value in a comment; none is ever written as a decimal.
--
-- Deliberately demonstrates the shape domain-controls.md's ancestor-chain
-- locking exists for: budget is owned at MORE than one level of the same
-- WBS chain (see "budget_control_cell" section) -- WBS-A-CIVIL *and* its
-- child WBS-A-CIVIL-FOUND both carry non-zero budget_paise for the same
-- head, and likewise WBS-A-PM / WBS-A-PM-MILL and WBS-B-PM / WBS-B-PM-MOD.

-- ============================================================ organisation
INSERT INTO organisation
    (organisation_id, code, name, base_currency, fy_start_month, created_by, updated_by)
VALUES
    ('ORG-DEMO', 'DEMO-GRP', 'Meridian Industries Group', 'INR', 4,
     'SEED-SCRIPT', 'SEED-SCRIPT');

INSERT INTO entity
    (entity_id, organisation_id, code, name, gst_no, pan_no, created_by, updated_by)
VALUES
    ('ENT-DM1', 'ORG-DEMO', 'MSPL', 'Meridian Steel & Power Ltd', NULL, NULL,
     'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('ENT-DM2', 'ORG-DEMO', 'MIPL', 'Meridian Infra Projects Ltd', NULL, NULL,
     'SEED-SCRIPT', 'SEED-SCRIPT');

-- ------------------------------------------------------ organisation hierarchy
INSERT INTO division (division_id, entity_id, code, name, created_by, updated_by)
VALUES
    ('DIV-DM1-OPS', 'ENT-DM1', 'OPS', 'Operations Division', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('DIV-DM2-OPS', 'ENT-DM2', 'OPS', 'Operations Division', 'SEED-SCRIPT', 'SEED-SCRIPT');

INSERT INTO branch (branch_id, entity_id, code, name, created_by, updated_by)
VALUES
    ('BR-DM1-HO', 'ENT-DM1', 'HO', 'Head Office', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('BR-DM2-HO', 'ENT-DM2', 'HO', 'Head Office', 'SEED-SCRIPT', 'SEED-SCRIPT');

INSERT INTO zone (zone_id, entity_id, code, name, created_by, updated_by)
VALUES
    ('ZN-DM1-E', 'ENT-DM1', 'EZ', 'Eastern Zone', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('ZN-DM1-W', 'ENT-DM1', 'WZ', 'Western Zone', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('ZN-DM2-E', 'ENT-DM2', 'EZ', 'Eastern Zone', 'SEED-SCRIPT', 'SEED-SCRIPT');

INSERT INTO plant (plant_id, entity_id, zone_id, code, name, created_by, updated_by)
VALUES
    ('PLT-DM1-A', 'ENT-DM1', 'ZN-DM1-E', 'PLT-A', 'Plant Alpha - Integrated Works',
     'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('PLT-DM1-B', 'ENT-DM1', 'ZN-DM1-W', 'PLT-B', 'Plant Beta - Rolling Mill',
     'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('PLT-DM2-A', 'ENT-DM2', 'ZN-DM2-E', 'PLT-C', 'Plant Gamma - Solar Park',
     'SEED-SCRIPT', 'SEED-SCRIPT');

-- Location is a FIRST-CLASS dimension here (AMB-07), not a synonym for
-- plant: LOC-DM1-HO deliberately carries no plant_id, showing it is its own
-- axis rather than always derived from one.
INSERT INTO location
    (location_id, entity_id, plant_id, code, name, address_line, city, state_code,
     created_by, updated_by)
VALUES
    ('LOC-DM1-A1', 'ENT-DM1', 'PLT-DM1-A', 'LOC-A1', 'Plant Alpha - Main Store Yard',
     'Yard Road 1', 'Demo City East', 'OD', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('LOC-DM1-A2', 'ENT-DM1', 'PLT-DM1-A', 'LOC-A2', 'Plant Alpha - Site Office',
     'Yard Road 2', 'Demo City East', 'OD', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('LOC-DM1-B1', 'ENT-DM1', 'PLT-DM1-B', 'LOC-B1', 'Plant Beta - Fabrication Yard',
     'Mill Road 1', 'Demo City West', 'OD', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('LOC-DM1-HO', 'ENT-DM1', NULL, 'LOC-HO1', 'Head Office - Procurement Cell',
     'Corporate Avenue 1', 'Demo City Central', 'OD', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('LOC-DM2-A1', 'ENT-DM2', 'PLT-DM2-A', 'LOC-C1', 'Plant Gamma - Solar Yard',
     'Solar Park Road 1', 'Demo City South', 'OD', 'SEED-SCRIPT', 'SEED-SCRIPT');

-- ------------------------------------------------------------------ identity
-- User ids mirror the SQLite prototype's DEV_USERS convention
-- (app/backend/auth.py) for continuity, one per role that matters:
--   U-REQ  Requestor            U-PFC  BudgetController + FinanceApprover
--   U-PM   Requestor + BudgetController      U-CFO  FinanceApprover + CapitalisationApprover
--   U-PLH  ProcurementApprover  U-AUD  Auditor
--   U-PROC ProcurementApprover  U-ADM  Administrator
--   U-FIN  FinanceApprover
-- Role GRANTS themselves are not modelled by this schema yet (Milestone 4);
-- this table only carries identity. SVC-ZOHO demonstrates the SERVICE
-- principal kind used by background integration adapters.
INSERT INTO app_user
    (user_id, email, display_name, principal_kind, created_by, updated_by)
VALUES
    ('U-REQ',  'r.iyer@meridian-industries.example',       'R. Iyer',        'USER',    'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-PM',   's.nair@meridian-industries.example',       'S. Nair',        'USER',    'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-PLH',  'a.verma@meridian-industries.example',      'A. Verma',       'USER',    'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-PROC', 'k.chatterjee@meridian-industries.example', 'K. Chatterjee',  'USER',    'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-FIN',  'm.gupta@meridian-industries.example',      'M. Gupta',       'USER',    'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-PFC',  'n.reddy@meridian-industries.example',      'N. Reddy',       'USER',    'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-CFO',  'v.krishnan@meridian-industries.example',   'V. Krishnan',    'USER',    'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-AUD',  'p.singh@meridian-industries.example',      'P. Singh',       'USER',    'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-ADM',  'admin@meridian-industries.example',        'Demo Administrator', 'USER', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    -- Fable 5.1 (2026-09-11): the Zoho ERP demo organisation's four users,
    -- WBS Administrators by the product owner's decision (see
    -- docs/fable51/DECISIONS_2026-09-11.md). Synthetic display names only.
    ('U-RAKESH',   'rakesh.s@rapguru.net',   'Rakesh Singh',       'USER', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-PRITHA',   'pritha.b@rapguru.net',   'Pritha Rapguru',     'USER', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-SURAJ',    'surajk.d@rapguru.net',   'Suraj',              'USER', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('U-ABHISHEK', 'abhishek.s@rapguru.net', 'Abhishek Sonthalia', 'USER', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('SVC-ZOHO', 'svc-zoho@meridian-industries.example',   'Zoho Sync Service',  'SERVICE', 'SEED-SCRIPT', 'SEED-SCRIPT');

-- --------------------------------------------------------------- calendar
-- One CLOSED, one OPEN, one FUTURE per entity, non-overlapping quarters of
-- FY2026-27 (fy_start_month = 4). The gist exclusion constraint on
-- accounting_period is what actually proves these never overlap.
INSERT INTO accounting_period
    (period_id, entity_id, period_start, period_end, state, closed_at, closed_by, created_by)
VALUES
    ('P-DM1-Q1', 'ENT-DM1', '2026-04-01', '2026-06-30', 'CLOSED',
     '2026-07-05T10:00:00+00:00', 'U-PFC', 'U-ADM'),
    ('P-DM1-Q2', 'ENT-DM1', '2026-07-01', '2026-09-30', 'OPEN', NULL, NULL, 'U-ADM'),
    ('P-DM1-Q3', 'ENT-DM1', '2026-10-01', '2026-12-31', 'FUTURE', NULL, NULL, 'U-ADM'),
    ('P-DM1-Q4', 'ENT-DM1', '2027-01-01', '2027-03-31', 'FUTURE', NULL, NULL, 'U-ADM'),
    ('P-DM2-Q1', 'ENT-DM2', '2026-04-01', '2026-06-30', 'CLOSED',
     '2026-07-05T10:00:00+00:00', 'U-PFC', 'U-ADM'),
    ('P-DM2-Q2', 'ENT-DM2', '2026-07-01', '2026-09-30', 'OPEN', NULL, NULL, 'U-ADM'),
    ('P-DM2-Q3', 'ENT-DM2', '2026-10-01', '2026-12-31', 'FUTURE', NULL, NULL, 'U-ADM');

-- ------------------------------------------------------------- budget heads
INSERT INTO budget_head (budget_head_id, entity_id, code, name, created_by, updated_by)
VALUES
    ('BH-DM1-CIVIL', 'ENT-DM1', 'CIVIL', 'Civil Works',        'U-ADM', 'U-ADM'),
    ('BH-DM1-PM',    'ENT-DM1', 'PM',    'Plant & Machinery',  'U-ADM', 'U-ADM'),
    ('BH-DM1-ELEC',  'ENT-DM1', 'ELEC',  'Electrical',         'U-ADM', 'U-ADM'),
    ('BH-DM2-PM',    'ENT-DM2', 'PM',    'Plant & Machinery',  'U-ADM', 'U-ADM'),
    ('BH-DM2-CIVIL', 'ENT-DM2', 'CIVIL', 'Civil Works',        'U-ADM', 'U-ADM');

-- ---------------------------------------------------------------- projects
INSERT INTO project
    (project_id, entity_id, plant_id, location_id, capex_code, name, status,
     created_at, created_by, updated_at, updated_by)
VALUES
    ('PRJ-DM-001', 'ENT-DM1', 'PLT-DM1-A', 'LOC-DM1-A1', 'CAPEX-DEMO-001',
     'Plant Alpha Capacity Expansion', 'Released',
     '2026-04-02T09:00:00+00:00', 'U-PM', '2026-04-02T09:00:00+00:00', 'U-PM'),
    ('PRJ-DM-002', 'ENT-DM2', 'PLT-DM2-A', 'LOC-DM2-A1', 'CAPEX-DEMO-002',
     'Plant Gamma Solar Park', 'Released',
     '2026-04-10T09:00:00+00:00', 'U-PM', '2026-04-10T09:00:00+00:00', 'U-PM');

-- ------------------------------------------------------------- WBS elements
-- Two projects, each a 3-level ltree hierarchy. Labels are the wbs_id
-- lower-cased with '-' -> '_' so they are ltree-safe (letters/digits/_).
--
-- PRJ-DM-001:
--   wbs_a_civil                                   (L1, BH-DM1-CIVIL)
--     wbs_a_civil.wbs_a_civil_found                (L2, BH-DM1-CIVIL -- 2nd owning level)
--       wbs_a_civil.wbs_a_civil_found.wbs_a_civil_found_pil   (L3)
--       wbs_a_civil.wbs_a_civil_found.wbs_a_civil_found_conc  (L3)
--     wbs_a_civil.wbs_a_civil_struct                (L2)
--   wbs_a_pm                                      (L1, BH-DM1-PM)
--     wbs_a_pm.wbs_a_pm_mill                        (L2, BH-DM1-PM -- 2nd owning level)
--       wbs_a_pm.wbs_a_pm_mill.wbs_a_pm_mill_install   (L3)
--   wbs_a_elec                                    (L1, BH-DM1-ELEC)
INSERT INTO wbs_element
    (wbs_id, project_id, parent_wbs_id, wbs_code, description, wbs_path, level,
     sort_order, budget_head_id, created_by, updated_by)
VALUES
    ('WBS-A-CIVIL', 'PRJ-DM-001', NULL, 'CAPEX-DEMO-001.01', 'Civil and Structural Work',
     'wbs_a_civil', 1, 1, 'BH-DM1-CIVIL', 'U-PM', 'U-PM'),
    ('WBS-A-CIVIL-FOUND', 'PRJ-DM-001', 'WBS-A-CIVIL', 'CAPEX-DEMO-001.01.01',
     'Foundation and Piling', 'wbs_a_civil.wbs_a_civil_found', 2, 1, 'BH-DM1-CIVIL',
     'U-PM', 'U-PM'),
    ('WBS-A-CIVIL-FOUND-PIL', 'PRJ-DM-001', 'WBS-A-CIVIL-FOUND', 'CAPEX-DEMO-001.01.01.01',
     'Site Piling Works', 'wbs_a_civil.wbs_a_civil_found.wbs_a_civil_found_pil', 3, 1,
     'BH-DM1-CIVIL', 'U-PM', 'U-PM'),
    ('WBS-A-CIVIL-FOUND-CONC', 'PRJ-DM-001', 'WBS-A-CIVIL-FOUND', 'CAPEX-DEMO-001.01.01.02',
     'Foundation Concreting', 'wbs_a_civil.wbs_a_civil_found.wbs_a_civil_found_conc', 3, 2,
     'BH-DM1-CIVIL', 'U-PM', 'U-PM'),
    ('WBS-A-CIVIL-STRUCT', 'PRJ-DM-001', 'WBS-A-CIVIL', 'CAPEX-DEMO-001.01.02',
     'Structural Steel Erection', 'wbs_a_civil.wbs_a_civil_struct', 2, 2, 'BH-DM1-CIVIL',
     'U-PM', 'U-PM'),
    ('WBS-A-PM', 'PRJ-DM-001', NULL, 'CAPEX-DEMO-001.02', 'Plant and Machinery',
     'wbs_a_pm', 1, 2, 'BH-DM1-PM', 'U-PM', 'U-PM'),
    ('WBS-A-PM-MILL', 'PRJ-DM-001', 'WBS-A-PM', 'CAPEX-DEMO-001.02.01',
     'Rolling Mill Equipment', 'wbs_a_pm.wbs_a_pm_mill', 2, 1, 'BH-DM1-PM', 'U-PM', 'U-PM'),
    ('WBS-A-PM-MILL-INSTALL', 'PRJ-DM-001', 'WBS-A-PM-MILL', 'CAPEX-DEMO-001.02.01.01',
     'Mill Installation and Commissioning', 'wbs_a_pm.wbs_a_pm_mill.wbs_a_pm_mill_install',
     3, 1, 'BH-DM1-PM', 'U-PM', 'U-PM'),
    ('WBS-A-ELEC', 'PRJ-DM-001', NULL, 'CAPEX-DEMO-001.03', 'Electrical Installations',
     'wbs_a_elec', 1, 3, 'BH-DM1-ELEC', 'U-PM', 'U-PM');

-- PRJ-DM-001 is seeded Released, and lifecycle_state (014) lets procurement
-- run only on a Released WBS element: a Draft element under a Released
-- project refused every purchase request on the UAT estate (found
-- 2026-09-12, LIFECYCLE_STATE). The demo estate's elements are Released.
UPDATE wbs_element SET status = 'Released' WHERE project_id = 'PRJ-DM-001';

-- PRJ-DM-002:
--   wbs_b_pm                                      (L1, BH-DM2-PM)
--     wbs_b_pm.wbs_b_pm_mod                         (L2, BH-DM2-PM -- 2nd owning level)
--       wbs_b_pm.wbs_b_pm_mod.wbs_b_pm_mod_arr          (L3)
--     wbs_b_pm.wbs_b_pm_inv                         (L2)
--   wbs_b_civil                                   (L1, BH-DM2-CIVIL)
INSERT INTO wbs_element
    (wbs_id, project_id, parent_wbs_id, wbs_code, description, wbs_path, level,
     sort_order, budget_head_id, created_by, updated_by)
VALUES
    ('WBS-B-PM', 'PRJ-DM-002', NULL, 'CAPEX-DEMO-002.01', 'Solar Modules and Inverters',
     'wbs_b_pm', 1, 1, 'BH-DM2-PM', 'U-PM', 'U-PM'),
    ('WBS-B-PM-MOD', 'PRJ-DM-002', 'WBS-B-PM', 'CAPEX-DEMO-002.01.01',
     'Module Installation', 'wbs_b_pm.wbs_b_pm_mod', 2, 1, 'BH-DM2-PM', 'U-PM', 'U-PM'),
    ('WBS-B-PM-MOD-ARR', 'PRJ-DM-002', 'WBS-B-PM-MOD', 'CAPEX-DEMO-002.01.01.01',
     'Array Mounting Structures', 'wbs_b_pm.wbs_b_pm_mod.wbs_b_pm_mod_arr', 3, 1,
     'BH-DM2-PM', 'U-PM', 'U-PM'),
    ('WBS-B-PM-INV', 'PRJ-DM-002', 'WBS-B-PM', 'CAPEX-DEMO-002.01.02',
     'Inverter Yard', 'wbs_b_pm.wbs_b_pm_inv', 2, 2, 'BH-DM2-PM', 'U-PM', 'U-PM'),
    ('WBS-B-CIVIL', 'PRJ-DM-002', NULL, 'CAPEX-DEMO-002.02', 'Balance of Plant Civil Works',
     'wbs_b_civil', 1, 2, 'BH-DM2-CIVIL', 'U-PM', 'U-PM');

-- ------------------------------------------------------- budget control cells
-- The lock target. budget_paise <> 0 is exactly what makes a cell
-- "budget-owning". Note WBS-A-CIVIL AND WBS-A-CIVIL-FOUND both own budget
-- for BH-DM1-CIVIL (same for WBS-A-PM/WBS-A-PM-MILL and WBS-B-PM/
-- WBS-B-PM-MOD) -- the multi-level-ownership shape the ancestor-chain
-- locking proof depends on. Leaf cells with no own budget still get a row
-- (budget_paise = 0) so their ledger cells below have something to key off.
INSERT INTO budget_control_cell (wbs_id, budget_head_id, budget_paise, updated_by)
VALUES
    ('WBS-A-CIVIL',            'BH-DM1-CIVIL', 800000000, 'U-PFC'), -- Rs 80,00,000
    ('WBS-A-CIVIL-FOUND',      'BH-DM1-CIVIL', 200000000, 'U-PFC'), -- Rs 20,00,000 (2nd owning level)
    ('WBS-A-CIVIL-FOUND-PIL',  'BH-DM1-CIVIL', 0,         'U-PFC'),
    ('WBS-A-CIVIL-FOUND-CONC', 'BH-DM1-CIVIL', 0,         'U-PFC'),
    ('WBS-A-CIVIL-STRUCT',     'BH-DM1-CIVIL', 0,         'U-PFC'),
    ('WBS-A-PM',               'BH-DM1-PM',    1200000000, 'U-PFC'), -- Rs 1,20,00,000
    ('WBS-A-PM-MILL',          'BH-DM1-PM',    300000000,  'U-PFC'), -- Rs 30,00,000 (2nd owning level)
    ('WBS-A-PM-MILL-INSTALL',  'BH-DM1-PM',    0,          'U-PFC'),
    ('WBS-A-ELEC',             'BH-DM1-ELEC',  400000000,  'U-PFC'), -- Rs 40,00,000
    ('WBS-B-PM',               'BH-DM2-PM',    900000000,  'U-PFC'), -- Rs 90,00,000
    ('WBS-B-PM-MOD',           'BH-DM2-PM',    250000000,  'U-PFC'), -- Rs 25,00,000 (2nd owning level)
    ('WBS-B-PM-MOD-ARR',       'BH-DM2-PM',    0,          'U-PFC'),
    ('WBS-B-PM-INV',           'BH-DM2-PM',    0,          'U-PFC'),
    ('WBS-B-CIVIL',            'BH-DM2-CIVIL', 300000000,  'U-PFC'); -- Rs 30,00,000

-- ------------------------------------------------------- budget ledger cells
-- Own values only -- rollups are computed at read time, never stored.
-- original_paise mirrors budget_paise at the budget-owning cells (kind =
-- 'ORIGINAL'); WBS-A-CIVIL additionally carries an approved revision, and
-- WBS-A-PM an amount effective in a FUTURE period, to exercise those columns.
-- Commitment/actual/received figures are internally consistent throughout:
--   ordered_paise == commitment_paise + actual_paise   (billed + open == ordered)
--   received_not_billed_paise is its own bucket, never folded into either.
INSERT INTO budget_ledger_cell
    (wbs_id, budget_head_id, original_paise, revisions_paise, future_budget_paise,
     ordered_paise, commitment_paise, actual_paise, received_paise,
     received_not_billed_paise, pr_reserved_paise, updated_by)
VALUES
    -- PRJ-DM-001 / BH-DM1-CIVIL chain
    ('WBS-A-CIVIL', 'BH-DM1-CIVIL',
     800000000, 50000000, 0,           -- original Rs 80,00,000; revision +Rs 5,00,000
     0, 0, 0, 0, 0, 0, 'U-PFC'),
    ('WBS-A-CIVIL-FOUND', 'BH-DM1-CIVIL',
     200000000, 0, 0,                  -- original Rs 20,00,000
     0, 0, 0, 0, 0, 0, 'U-PFC'),
    ('WBS-A-CIVIL-FOUND-PIL', 'BH-DM1-CIVIL',
     0, 0, 0,
     60000000, 40000000, 20000000, 30000000, 10000000, 10000000, 'U-PROC'),
     -- ordered Rs 6,00,000; commitment Rs 4,00,000; actual (billed) Rs 2,00,000;
     -- received Rs 3,00,000 (so received_not_billed = Rs 1,00,000); PR reserved Rs 1,00,000
    ('WBS-A-CIVIL-FOUND-CONC', 'BH-DM1-CIVIL',
     0, 0, 0,
     40000000, 0, 40000000, 40000000, 0, 0, 'U-PROC'),
     -- ordered = actual = received = Rs 4,00,000, fully billed and received
    ('WBS-A-CIVIL-STRUCT', 'BH-DM1-CIVIL',
     0, 0, 0,
     50000000, 50000000, 0, 0, 0, 10000000, 'U-PROC'),
     -- ordered Rs 5,00,000, nothing billed yet; PR reserved Rs 1,00,000
    -- PRJ-DM-001 / BH-DM1-PM chain
    ('WBS-A-PM', 'BH-DM1-PM',
     1200000000, 0, 100000000,         -- original Rs 1,20,00,000; Rs 10,00,000 effective in a FUTURE period
     0, 0, 0, 0, 0, 0, 'U-PFC'),
    ('WBS-A-PM-MILL', 'BH-DM1-PM',
     300000000, 0, 0,                  -- original Rs 30,00,000
     0, 0, 0, 0, 0, 0, 'U-PFC'),
    ('WBS-A-PM-MILL-INSTALL', 'BH-DM1-PM',
     0, 0, 0,
     150000000, 100000000, 50000000, 80000000, 30000000, 20000000, 'U-PROC'),
     -- ordered Rs 15,00,000; commitment Rs 10,00,000; actual Rs 5,00,000;
     -- received Rs 8,00,000 (received_not_billed Rs 3,00,000); PR reserved Rs 2,00,000
    -- PRJ-DM-001 / BH-DM1-ELEC (no children -- spend recorded directly here)
    ('WBS-A-ELEC', 'BH-DM1-ELEC',
     400000000, 0, 0,                  -- original Rs 40,00,000
     100000000, 40000000, 60000000, 70000000, 10000000, 0, 'U-PROC'),
     -- ordered Rs 10,00,000; commitment Rs 4,00,000; actual Rs 6,00,000;
     -- received Rs 7,00,000 (received_not_billed Rs 1,00,000)
    -- PRJ-DM-002 / BH-DM2-PM chain
    ('WBS-B-PM', 'BH-DM2-PM',
     900000000, 0, 0,                  -- original Rs 90,00,000
     0, 0, 0, 0, 0, 0, 'U-PFC'),
    ('WBS-B-PM-MOD', 'BH-DM2-PM',
     250000000, 0, 0,                  -- original Rs 25,00,000
     0, 0, 0, 0, 0, 0, 'U-PFC'),
    ('WBS-B-PM-MOD-ARR', 'BH-DM2-PM',
     0, 0, 0,
     120000000, 80000000, 40000000, 50000000, 10000000, 15000000, 'U-PROC'),
     -- ordered Rs 12,00,000; commitment Rs 8,00,000; actual Rs 4,00,000;
     -- received Rs 5,00,000 (received_not_billed Rs 1,00,000); PR reserved Rs 1,50,000
    ('WBS-B-PM-INV', 'BH-DM2-PM',
     0, 0, 0,
     60000000, 0, 60000000, 60000000, 0, 0, 'U-PROC'),
     -- ordered = actual = received = Rs 6,00,000, fully billed and received
    -- PRJ-DM-002 / BH-DM2-CIVIL (no children -- spend recorded directly here)
    ('WBS-B-CIVIL', 'BH-DM2-CIVIL',
     300000000, 0, 0,                  -- original Rs 30,00,000
     80000000, 50000000, 30000000, 40000000, 10000000, 10000000, 'U-PROC');
     -- ordered Rs 8,00,000; commitment Rs 5,00,000; actual Rs 3,00,000;
     -- received Rs 4,00,000 (received_not_billed Rs 1,00,000); PR reserved Rs 1,00,000

-- --------------------------------------------------------------------- audit
-- Two independent hash-chained streams, built with the exact algorithm
-- app.backend.pg.audit.compute_entry_hash uses (frozen payload format
-- `prev|at|actor|action|type|id|detail`), so app.backend.pg.audit.verify_chain
-- reports both intact. Each `at` is written as an explicit UTC literal with
-- no fractional seconds so it round-trips, via canonical_at(), to the exact
-- same ISO string used to compute the hash below -- pgcrypto's digest()
-- takes the text argument directly (no bytea cast needed; pgcrypto ships a
-- text overload).
--
-- Stream 1: PROJECT:PRJ-DM-001
INSERT INTO audit_log
    (stream_key, seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash)
VALUES (
    'PROJECT:PRJ-DM-001', 1, '2026-04-02T09:00:00+00:00'::timestamptz,
    'U-ADM', 'CREATE', 'PROJECT', 'PRJ-DM-001',
    'Project created: Plant Alpha Capacity Expansion',
    NULL,
    encode(digest(
        '|2026-04-02T09:00:00+00:00|U-ADM|CREATE|PROJECT|PRJ-DM-001|' ||
        'Project created: Plant Alpha Capacity Expansion', 'sha256'), 'hex')
);

WITH prev AS (
    SELECT entry_hash FROM audit_log WHERE stream_key = 'PROJECT:PRJ-DM-001' AND seq = 1
)
INSERT INTO audit_log
    (stream_key, seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash)
SELECT
    'PROJECT:PRJ-DM-001', 2, '2026-04-03T10:15:00+00:00'::timestamptz,
    'U-PM', 'WBS_CREATE', 'WBS', 'WBS-A-CIVIL',
    'WBS element created: Civil and Structural Work',
    prev.entry_hash,
    encode(digest(
        prev.entry_hash || '|2026-04-03T10:15:00+00:00|U-PM|WBS_CREATE|WBS|WBS-A-CIVIL|' ||
        'WBS element created: Civil and Structural Work', 'sha256'), 'hex')
FROM prev;

WITH prev AS (
    SELECT entry_hash FROM audit_log WHERE stream_key = 'PROJECT:PRJ-DM-001' AND seq = 2
)
INSERT INTO audit_log
    (stream_key, seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash)
SELECT
    'PROJECT:PRJ-DM-001', 3, '2026-04-05T11:30:00+00:00'::timestamptz,
    'U-PFC', 'BUDGET_SET', 'BUDGET_CELL', 'WBS-A-CIVIL:BH-DM1-CIVIL',
    'Original budget approved: INR 80,00,000',
    prev.entry_hash,
    encode(digest(
        prev.entry_hash || '|2026-04-05T11:30:00+00:00|U-PFC|BUDGET_SET|BUDGET_CELL|' ||
        'WBS-A-CIVIL:BH-DM1-CIVIL|Original budget approved: INR 80,00,000', 'sha256'), 'hex')
FROM prev;

WITH prev AS (
    SELECT entry_hash FROM audit_log WHERE stream_key = 'PROJECT:PRJ-DM-001' AND seq = 3
)
INSERT INTO audit_log
    (stream_key, seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash)
SELECT
    'PROJECT:PRJ-DM-001', 4, '2026-06-15T14:00:00+00:00'::timestamptz,
    'U-CFO', 'REVISION_APPROVE', 'BUDGET_CELL', 'WBS-A-CIVIL:BH-DM1-CIVIL',
    'Revision approved: +INR 5,00,000',
    prev.entry_hash,
    encode(digest(
        prev.entry_hash || '|2026-06-15T14:00:00+00:00|U-CFO|REVISION_APPROVE|BUDGET_CELL|' ||
        'WBS-A-CIVIL:BH-DM1-CIVIL|Revision approved: +INR 5,00,000', 'sha256'), 'hex')
FROM prev;

-- Stream 2: PROJECT:PRJ-DM-002
INSERT INTO audit_log
    (stream_key, seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash)
VALUES (
    'PROJECT:PRJ-DM-002', 1, '2026-04-10T09:00:00+00:00'::timestamptz,
    'U-ADM', 'CREATE', 'PROJECT', 'PRJ-DM-002',
    'Project created: Plant Gamma Solar Park',
    NULL,
    encode(digest(
        '|2026-04-10T09:00:00+00:00|U-ADM|CREATE|PROJECT|PRJ-DM-002|' ||
        'Project created: Plant Gamma Solar Park', 'sha256'), 'hex')
);

WITH prev AS (
    SELECT entry_hash FROM audit_log WHERE stream_key = 'PROJECT:PRJ-DM-002' AND seq = 1
)
INSERT INTO audit_log
    (stream_key, seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash)
SELECT
    'PROJECT:PRJ-DM-002', 2, '2026-04-12T09:45:00+00:00'::timestamptz,
    'U-PM', 'WBS_CREATE', 'WBS', 'WBS-B-PM',
    'WBS element created: Solar Modules and Inverters',
    prev.entry_hash,
    encode(digest(
        prev.entry_hash || '|2026-04-12T09:45:00+00:00|U-PM|WBS_CREATE|WBS|WBS-B-PM|' ||
        'WBS element created: Solar Modules and Inverters', 'sha256'), 'hex')
FROM prev;

WITH prev AS (
    SELECT entry_hash FROM audit_log WHERE stream_key = 'PROJECT:PRJ-DM-002' AND seq = 2
)
INSERT INTO audit_log
    (stream_key, seq, at, actor, action, object_type, object_id, detail, prev_hash, entry_hash)
SELECT
    'PROJECT:PRJ-DM-002', 3, '2026-04-14T13:20:00+00:00'::timestamptz,
    'U-PFC', 'BUDGET_SET', 'BUDGET_CELL', 'WBS-B-PM:BH-DM2-PM',
    'Original budget approved: INR 90,00,000',
    prev.entry_hash,
    encode(digest(
        prev.entry_hash || '|2026-04-14T13:20:00+00:00|U-PFC|BUDGET_SET|BUDGET_CELL|' ||
        'WBS-B-PM:BH-DM2-PM|Original budget approved: INR 90,00,000', 'sha256'), 'hex')
FROM prev;
