-- 009_budget_categories.sql
-- Fable 5.1 demo estate for migration 026: budget CATEGORIES (a dimension
-- separate from budget_head -- AMB-04, product-owner decision), an ACTIVE
-- organisation-wide approval workflow for ORIGINAL_BUDGET, one required and
-- one optional budget custom field (REQ-BUD-020), and a classification of
-- the cells seed_parts/003_budget.sql already funded so the category filter
-- has something to show on day one.
--
-- Loaded by app/backend/pg/seed.py in filename order, AFTER 003 (cells and
-- ORIGINAL lines), 004 (users and roles) and 008 (reason codes, SYSTEM user).
-- Nothing here invents an identity: U-ADM, U-FIN, ENT-DM1, PRJ-DM-001 and the
-- WBS/head ids all come from seed_demo.sql and 003.
--
-- NO original_budget DOCUMENT IS SEEDED, deliberately. The demo's point is
-- that the product owner creates one through the New Budget screen, submits
-- it as U-REQ, approves it as U-FIN, and watches it release -- a seeded
-- document would be a document nobody created through the controls, and its
-- audit stream (which contributor_set reads) cannot be fabricated in SQL
-- without reimplementing the hash chain.

-- ============================================================ categories
-- Two levels: a parent per asset family, children where the intake PDF's
-- category-wise report would want the split. All estate-wide (entity NULL)
-- except one deliberately ENT-DM2-only category, so the entity-restriction
-- branch of the RLS policy is exercised by real data.
INSERT INTO budget_category
    (category_id, code, name, description, parent_category_id, display_order,
     active, entity_id, created_by, updated_by)
VALUES
    ('BC-PLANT',     'PLANT-MACHINERY', 'Plant & Machinery',
     'Production plant, process equipment and installation', NULL, 10, true, NULL, 'U-ADM', 'U-ADM'),
    ('BC-PLANT-PROC','PLANT-PROCESS',   'Process equipment',
     'Kilns, presses, reactors and their controls', 'BC-PLANT', 11, true, NULL, 'U-ADM', 'U-ADM'),
    ('BC-PLANT-MH',  'PLANT-MATERIAL-HANDLING', 'Material handling',
     'Conveyors, cranes, hoists', 'BC-PLANT', 12, true, NULL, 'U-ADM', 'U-ADM'),
    ('BC-CIVIL',     'CIVIL-STRUCTURAL', 'Civil & Structural',
     'Foundations, sheds, buildings, roads', NULL, 20, true, NULL, 'U-ADM', 'U-ADM'),
    ('BC-ELEC',      'ELECTRICAL',      'Electrical & Instrumentation',
     'Substation, switchgear, cabling, instrumentation', NULL, 30, true, NULL, 'U-ADM', 'U-ADM'),
    ('BC-UTIL',      'UTILITIES',       'Utilities',
     'Water, compressed air, HVAC, fire protection', NULL, 40, true, NULL, 'U-ADM', 'U-ADM'),
    ('BC-IT',        'IT-AUTOMATION',   'IT & Automation',
     'Plant IT, SCADA, MES, networks', NULL, 50, true, NULL, 'U-ADM', 'U-ADM'),
    ('BC-PRELIM',    'PRELIMINARY',     'Preliminary & Pre-operative',
     'Design, consultancy, approvals, project management', NULL, 60, true, NULL, 'U-ADM', 'U-ADM'),
    ('BC-SOLAR-DM2', 'SOLAR-PARK',      'Solar park (ENT-DM2 only)',
     'Applicable only to the solar entity; exercises the entity-restricted branch of the policy',
     NULL, 70, true, 'ENT-DM2', 'U-ADM', 'U-ADM'),
    ('BC-RETIRED',   'LEGACY-RETIRED',  'Retired category',
     'Inactive; must be refused on a new line', NULL, 999, false, NULL, 'U-ADM', 'U-ADM');

-- ============================================================ classify seeded cells
-- The cells 003 funded pre-date the category dimension. Classifying them
-- here is the ONE permitted transition (NULL -> value); the trigger refuses
-- any later change. Lines are classified to match, so the
-- category-matches-cell trigger holds over the seed too.
UPDATE budget_control_cell SET budget_category_id = 'BC-PLANT'
 WHERE budget_category_id IS NULL AND budget_head_id IN ('BH-DM1-PM', 'BH-DM2-PM');
UPDATE budget_control_cell SET budget_category_id = 'BC-CIVIL'
 WHERE budget_category_id IS NULL AND budget_head_id IN ('BH-DM1-CIVIL', 'BH-DM2-CIVIL');
UPDATE budget_control_cell SET budget_category_id = 'BC-ELEC'
 WHERE budget_category_id IS NULL AND budget_head_id = 'BH-DM1-ELEC';
UPDATE budget_line bl SET budget_category_id = c.budget_category_id
  FROM budget_control_cell c
 WHERE c.wbs_id = bl.wbs_id AND c.budget_head_id = bl.budget_head_id
   AND bl.budget_category_id IS NULL AND c.budget_category_id IS NOT NULL
   AND bl.kind <> 'ORIGINAL';
-- kind='ORIGINAL' rows are immutable under 003's trigger, so their category
-- stays NULL in the seed: reporting reads the CELL's category, which is set.

-- ============================================================ custom fields (REQ-BUD-020)
INSERT INTO custom_field_def
    (field_def_id, code, label, data_type, select_options, is_required, is_active,
     created_by, updated_by)
VALUES
    ('CF-BUD-ASSET-CLASS', 'ASSET_CLASS', 'Asset class (Companies Act schedule)', 'SELECT',
     '["PLANT_MACHINERY_GENERAL", "PLANT_MACHINERY_CONTINUOUS", "BUILDINGS_FACTORY", "ELECTRICAL_INSTALLATIONS", "COMPUTERS", "OFFICE_EQUIPMENT"]'::jsonb,
     true, true, 'U-ADM', 'U-ADM'),
    ('CF-BUD-BOARD-REF', 'BOARD_APPROVAL_REF', 'Board approval reference', 'TEXT',
     NULL, false, true, 'U-ADM', 'U-ADM')
ON CONFLICT (code) DO NOTHING;

INSERT INTO custom_field_applicability
    (applicability_id, field_def_id, applies_to, is_active, created_by)
VALUES
    ('CFA-BUD-ASSET-CLASS', 'CF-BUD-ASSET-CLASS', 'BUDGET', true, 'U-ADM'),
    ('CFA-BUD-BOARD-REF',   'CF-BUD-BOARD-REF',   'BUDGET', true, 'U-ADM')
ON CONFLICT (field_def_id, applies_to) DO NOTHING;

-- ============================================================ approval workflow
-- Organisation-wide, ACTIVE: every original budget takes a single Finance
-- gate (any one Finance holder), with a 72-hour SLA. The maker cannot be
-- the approver whatever role they hold -- that is the engine's
-- contributor_set, not this definition. A large budget (over Rs 5 crore)
-- additionally needs the CFO: stage 2 applies_when the document total
-- exceeds 500,00,00,000 paise. MONEY IS INTEGER PAISE inside the predicate.
INSERT INTO approval_definition
    (definition_id, object_type, code, version, status, entity_id,
     effective_from, effective_to, created_by, activated_at, activated_by)
VALUES
    ('APD-OB-ORG-V1', 'ORIGINAL_BUDGET', 'OB-ORGWIDE', 1, 'ACTIVE',
     NULL, '2026-04-01', NULL, 'U-ADM', '2026-04-01T00:00:00+00:00', 'U-ADM');

INSERT INTO approval_rule
    (rule_id, definition_id, priority, predicate, description, created_by)
VALUES
    ('APR-OB-ORG-V1-STD', 'APD-OB-ORG-V1', 100,
     '{"all": [
         {"field": "object_type", "op": "eq", "value": "ORIGINAL_BUDGET"}
     ], "route": "STANDARD"}'::jsonb,
     'Every original budget, in every entity.', 'U-ADM');

INSERT INTO approval_stage
    (stage_id, definition_id, stage_no, name, parallel_group,
     quorum_type, quorum_n, applies_when, sla_hours, escalate_after_hours,
     escalate_to, allow_delegation, requires_reason, reason_code_set,
     created_by)
VALUES
    ('APS-OB-ORG-V1-1', 'APD-OB-ORG-V1', 1, 'Finance approval', NULL,
     'ANY', NULL, NULL, 72, NULL, NULL, true, false, NULL, 'U-ADM'),
    ('APS-OB-ORG-V1-2', 'APD-OB-ORG-V1', 2, 'CFO approval (over Rs 5 crore)', NULL,
     'ANY', NULL,
     '{"field": "amount_paise", "op": "gt", "value": 5000000000}'::jsonb,
     72, NULL, NULL, true, false, NULL, 'U-ADM');

INSERT INTO approval_stage_approver
    (stage_id, ordinal, approver_kind, approver_ref, scope_expr)
VALUES
    ('APS-OB-ORG-V1-1', 1, 'ROLE', 'Finance', NULL),
    ('APS-OB-ORG-V1-1', 2, 'ROLE', 'Project Finance Controller', NULL),
    ('APS-OB-ORG-V1-2', 1, 'ROLE', 'CFO', NULL);
