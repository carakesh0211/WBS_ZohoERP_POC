-- 004_access.sql
-- Demo role and scope grants for 004_identity_scope.sql's tables.
--
-- Loaded by the lead AFTER seed_demo.sql (see migrations/pg/seed_parts/README.md
-- and app/backend/pg/seed.py) -- every user_id referenced below except
-- U-NOGRANT already exists in seed_demo.sql's `app_user` insert.
--
-- Demonstrates five genuinely different scope shapes, deliberately not just
-- five different id sets:
--   * U-PFC     entity-scoped      (restricted to ENT-DM1 only)
--   * U-PLH     plant-scoped       (restricted to PLT-DM1-A only)
--   * U-PM      project-scoped     (restricted to PRJ-DM-001 only)
--   * U-ADM     read_all           (the explicit whole-estate override)
--   * U-NOGRANT empty grant set    (every dimension RESTRICTED to zero ids
--                                   -- holds a real role, sees nothing; the
--                                   "no grants" case made concrete rather
--                                   than left theoretical)
-- plus U-CFO, deliberately left with NO user_scope_restriction rows at all
-- and read_all = false: the "never configured" case, which resolves to
-- fully unrestricted on every dimension (Scope's own default) -- a THIRD,
-- distinct kind of "sees everything", next to U-ADM's explicit read_all.
-- tests/test_pg_roles.py and tests/test_pg_rls.py exercise all three shapes
-- against both repo.compile_scope and live RLS.
--
-- All ids referenced below (ENT-DM1, ENT-DM2, PLT-DM1-A, PLT-DM2-A,
-- PRJ-DM-001, LOC-DM1-A1, LOC-DM2-A1) are seed_demo.sql rows; nothing here
-- invents a new entity/plant/project/location.

-- ------------------------------------------------------------------ U-NOGRANT
-- A new demo identity: holds a real role (so `access.grant` /
-- `access.read`-style checks have something to exercise) but is deliberately
-- granted ZERO ids on every dimension -- see the header note above.
INSERT INTO app_user (user_id, email, display_name, principal_kind, created_by, updated_by)
VALUES ('U-NOGRANT', 'no-grant.demo@meridian-industries.example',
        'Demo No-Grant User', 'USER', 'SEED-SCRIPT', 'SEED-SCRIPT');

-- ------------------------------------------------------------------- roles
INSERT INTO role_grant (user_id, role, granted_by) VALUES
    ('U-REQ',      'Requestor',                   'U-ADM'),
    ('U-PM',       'Project Manager',              'U-ADM'),
    ('U-PLH',      'Plant Head',                   'U-ADM'),
    ('U-PROC',     'Procurement',                  'U-ADM'),
    ('U-FIN',      'Finance',                       'U-ADM'),
    ('U-PFC',      'Project Finance Controller',    'U-ADM'),
    ('U-CFO',      'CFO',                            'U-ADM'),
    ('U-AUD',      'Internal Auditor',               'U-ADM'),
    ('U-ADM',      'System Administrator',           'U-ADM'),
    -- Fable 5.1 (2026-09-11): the ERP demo organisation's users, Administrators.
    ('U-RAKESH',   'System Administrator',           'U-ADM'),
    ('U-PRITHA',   'System Administrator',           'U-ADM'),
    ('U-SURAJ',    'System Administrator',           'U-ADM'),
    ('U-ABHISHEK', 'System Administrator',           'U-ADM'),
    ('U-NOGRANT',  'Read-only Management User',      'U-ADM'),
    -- SVC-ZOHO is a SERVICE principal (seed_demo.sql). It holds an ordinary,
    -- non-maker-checker role -- Procurement carries only budget.read/
    -- po.amend/po.cancel/po.close in app.backend.pg.roles.PERMISSIONS, none
    -- of which is in MAKER_CHECKER. A SERVICE principal may never hold a
    -- maker-checker permission (see roles.assert_role_grantable /
    -- roles.assert_no_service_maker_checker); this row demonstrates
    -- COMPLIANCE with that rule, not an exception to it.
    ('SVC-ZOHO',   'Procurement',                    'U-ADM');

-- -------------------------------------------------------------- read_all flag
INSERT INTO user_access_flag (user_id, read_all, updated_by) VALUES
    ('U-ADM', true, 'U-ADM'),   -- System Administrator: explicit whole-estate read
    ('U-AUD', true, 'U-ADM'),   -- Internal Auditor: explicit whole-estate read
    -- Fable 5.1 (2026-09-11): the four ERP demo users, whole-estate read as Administrators.
    ('U-RAKESH',   true, 'U-ADM'),
    ('U-PRITHA',   true, 'U-ADM'),
    ('U-SURAJ',    true, 'U-ADM'),
    ('U-ABHISHEK', true, 'U-ADM');
-- No row at all for U-CFO, U-PFC, U-PLH, U-PM, U-NOGRANT, U-REQ, U-PROC,
-- U-FIN, SVC-ZOHO: read_all defaults to false for all of them, per
-- user_access_flag's own DEFAULT and roles.resolve_scope's handling of a
-- missing row.

-- ------------------------------------------------------------ scope: U-PFC
-- Entity-scoped: sees ENT-DM1 (and everything under it) only.
INSERT INTO user_scope_restriction (user_id, dimension, updated_by) VALUES
    ('U-PFC', 'entity', 'U-ADM');
INSERT INTO user_scope_grant (user_id, dimension, scope_value, granted_by) VALUES
    ('U-PFC', 'entity', 'ENT-DM1', 'U-ADM');

-- ------------------------------------------------------------ scope: U-PLH
-- Plant-scoped: sees PLT-DM1-A (and everything under it) only.
INSERT INTO user_scope_restriction (user_id, dimension, updated_by) VALUES
    ('U-PLH', 'plant', 'U-ADM');
INSERT INTO user_scope_grant (user_id, dimension, scope_value, granted_by) VALUES
    ('U-PLH', 'plant', 'PLT-DM1-A', 'U-ADM');

-- ------------------------------------------------------------- scope: U-PM
-- Project-scoped: sees PRJ-DM-001 only, regardless of entity/plant.
INSERT INTO user_scope_restriction (user_id, dimension, updated_by) VALUES
    ('U-PM', 'project', 'U-ADM');
INSERT INTO user_scope_grant (user_id, dimension, scope_value, granted_by) VALUES
    ('U-PM', 'project', 'PRJ-DM-001', 'U-ADM');

-- ------------------------------------------------------------ scope: U-PROC
-- Plant-scoped to the second entity's plant, for cross-entity contrast with
-- U-PLH.
INSERT INTO user_scope_restriction (user_id, dimension, updated_by) VALUES
    ('U-PROC', 'plant', 'U-ADM');
INSERT INTO user_scope_grant (user_id, dimension, scope_value, granted_by) VALUES
    ('U-PROC', 'plant', 'PLT-DM2-A', 'U-ADM');

-- ------------------------------------------------------------- scope: U-FIN
-- Entity-scoped to the second entity, for cross-entity contrast with U-PFC.
INSERT INTO user_scope_restriction (user_id, dimension, updated_by) VALUES
    ('U-FIN', 'entity', 'U-ADM');
INSERT INTO user_scope_grant (user_id, dimension, scope_value, granted_by) VALUES
    ('U-FIN', 'entity', 'ENT-DM2', 'U-ADM');

-- --------------------------------------------------------- scope: U-NOGRANT
-- Every dimension explicitly RESTRICTED, with ZERO permitted values in each
-- -- the empty-grant-set case. Holds a real role (Read-only Management
-- User) but must see NO rows anywhere repo.compile_scope or RLS applies,
-- proving "no grants -> no rows", never "no grants -> all rows".
INSERT INTO user_scope_restriction (user_id, dimension, updated_by) VALUES
    ('U-NOGRANT', 'entity',   'U-ADM'),
    ('U-NOGRANT', 'plant',    'U-ADM'),
    ('U-NOGRANT', 'project',  'U-ADM'),
    ('U-NOGRANT', 'location', 'U-ADM');
-- Deliberately no user_scope_grant rows for U-NOGRANT on any dimension.

-- U-REQ, U-CFO, U-AUD, U-ADM, SVC-ZOHO: no user_scope_restriction rows on
-- any dimension -- unrestricted (Scope's own `None` default) on every
-- dimension left unconfigured. For U-ADM and U-AUD this is moot (read_all
-- overrides); for U-CFO it is the point (see header note); for U-REQ and
-- SVC-ZOHO it simply reflects that this demo did not need to restrict them
-- further.
