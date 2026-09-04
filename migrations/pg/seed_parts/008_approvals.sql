-- 008_approvals.sql
-- Demo approval estate for 008_approval_engine.sql's tables.
--
-- Loaded by the lead AFTER seed_demo.sql (see migrations/pg/seed_parts/README.md
-- and app/backend/pg/seed.py, which discovers fragments in filename order).
-- Every user_id, entity_id and role name referenced below already exists:
-- users and entities from seed_demo.sql, roles from seed_parts/004_access.sql
-- and research/30_contracts/C9_roles.json. Nothing here invents an identity.
--
-- WHAT THIS ESTATE DEMONSTRATES
--
-- One ACTIVE purchase-requisition workflow, `PR-STANDARD` v1, scoped to
-- ENT-DM1, carrying every routing shape the engine has to support:
--
--   stage 1  Project Manager             ALL      a plain sequential gate
--   stage 2  Plant Head                  ALL      \  parallel_group PG-OPS:
--   stage 3  Procurement                 ALL      /  both open together
--   stage 4  CAPEX Committee             N_OF_M   any 2 of the committee,
--                                                 with an SLA and an
--                                                 escalation to the CFO
--   stage 5  Finance exception           ALL      applies_when: over budget
--
-- ...plus a DRAFT `PR-STANDARD` v2, so the versioning story is visible in the
-- demo rather than only in the tests: v1 is live and frozen, v2 is editable,
-- and activating v2 is what "changing the workflow" means. Contract 1's rule
-- is that a live definition is never edited, and a demo that shipped only one
-- version could not show what to do instead.
--
-- THE OVER-BUDGET ROUTE, WHICH IS THE POINT OF THE SCENARIO
--
-- Two mechanisms cooperate and they are deliberately different things:
--
--   * `approval_rule` priority 10 matches a requisition whose value exceeds
--     50,00,000 paise-denominated rupees and marks the routing `EXCEPTION`.
--     Priority 100 is the catch-all. Rules are evaluated in priority order and
--     the FIRST match wins, which is why UNIQUE (definition_id, priority)
--     exists -- a tie would leave the route decided by the query planner.
--   * `approval_stage` 5 carries `applies_when`, so the Finance exception gate
--     opens for an over-budget requisition and is recorded SKIPPED, with a
--     skip_reason, for one that is within budget. It is never simply absent:
--     an auditor must be able to see the control that did not fire.
--
-- MONEY IS INTEGER PAISE. The threshold below is 500000000 paise = Rs 50,00,000
-- exactly. It sits inside a jsonb predicate rather than a `*_paise` column, so
-- it is not reachable by the migration runner's money-type check -- stated here
-- because that check is what normally guarantees this discipline.
--
-- NO INSTANCES, ACTIONS OR ASSIGNMENTS ARE SEEDED, DELIBERATELY.
-- `approval_action` is hash-chained on stream `approval:{instance_id}` using
-- the frozen payload format `prev|at|actor|action|type|id|detail` (Contract 9).
-- A seed script cannot fabricate a valid `prev_hash`/`entry_hash` pair without
-- reimplementing app/backend/pg/audit.py's chaining in SQL, and a chain that is
-- wrong at row one is worse than no chain: it makes verification fail for a
-- reason that has nothing to do with tampering. Instances are created by the
-- engine, in a transaction, with the chain written alongside the state change.
-- This fragment seeds the CONFIGURATION the engine routes against.

-- ============================================================= reason codes
-- The controlled vocabulary a decision may cite. `applies_to_object_type` is
-- NULL on all of these: a reason such as BUDGET_EXCEEDED means the same thing
-- against a requisition and a budget revision, and one row per object type
-- would multiply the vocabulary without adding meaning.
--
-- OTHER is the only code with `requires_free_text`: every other code says what
-- it means on its own, and forcing a comment beside a self-explanatory reason
-- trains approvers to type "n/a".
-- ---------------------------------------------------------------- SYSTEM
-- The engine attributes system-initiated actions -- an auto-supersede when a
-- document changes under an open instance, an SLA escalation -- to SYSTEM,
-- and `approval_action.actor_user_id` is a foreign key to `app_user`.
--
-- So SYSTEM has to BE a principal rather than a magic string. Plan section
-- 10.3 is explicit: background principals are real rows with
-- `principal_kind = 'SERVICE'`, never an implicit "no user" bypass. A real
-- row also means an auditor reading the chain sees an actor they can look up,
-- and means these actions carry the same foreign-key integrity as a human's.
--
-- It is granted no role, so it holds no permission and can act through no
-- API. It exists solely to be attributable.
INSERT INTO app_user (user_id, email, display_name, principal_kind,
                      created_by, updated_by)
VALUES ('SYSTEM', 'system@capex.invalid', 'System', 'SERVICE', 'SEED', 'SEED')
ON CONFLICT (user_id) DO NOTHING;


INSERT INTO reason_code
    (code, applies_to_action, applies_to_object_type, label,
     requires_free_text, active, created_by)
VALUES
    ('BUDGET_EXCEEDED',    'APPROVE',  NULL,
     'Approved above available budget',                    false, true, 'U-ADM'),
    ('URGENT_BUSINESS_NEED', 'APPROVE', NULL,
     'Urgent business need',                               false, true, 'U-ADM'),
    ('WITHIN_DELEGATION',  'APPROVE',  NULL,
     'Within delegated authority',                         false, true, 'U-ADM'),
    ('INSUFFICIENT_JUSTIFICATION', 'REJECT', NULL,
     'Insufficient justification',                         false, true, 'U-ADM'),
    ('VENDOR_RISK',        'REJECT',   NULL,
     'Vendor risk not accepted',                           false, true, 'U-ADM'),
    ('DUPLICATE_REQUEST',  'REJECT',   NULL,
     'Duplicate of an existing request',                   false, true, 'U-ADM'),
    ('SPEC_INCOMPLETE',    'RETURN',   NULL,
     'Specification incomplete',                           false, true, 'U-ADM'),
    ('COSTING_UNCLEAR',    'RETURN',   NULL,
     'Costing or quantity basis unclear',                  false, true, 'U-ADM'),
    ('WRONG_WBS',          'RETURN',   NULL,
     'Charged to the wrong WBS element',                   false, true, 'U-ADM'),
    ('SUPERSEDED_BY_REVISION', 'RECALL', NULL,
     'Recalled: superseded by a revision',                 false, true, 'U-ADM'),
    ('NO_LONGER_REQUIRED', 'CANCEL',   NULL,
     'Cancelled: no longer required',                      false, true, 'U-ADM'),
    ('CORRECTED_AND_RESUBMITTED', 'RESUBMIT', NULL,
     'Corrected and resubmitted',                          false, true, 'U-ADM'),
    ('PLANNED_ABSENCE',    'DELEGATE', NULL,
     'Delegated for a planned absence',                    false, true, 'U-ADM'),
    -- Retired, not deleted: it was in use, so every historical action citing
    -- it must stay readable. `active = false` removes it from tomorrow's
    -- pick-list and from ix_reason_code_action, which is partial on `active`.
    ('LEGACY_MANUAL_OVERRIDE', 'APPROVE', NULL,
     'Legacy manual override (retired)',                   false, false, 'U-ADM'),
    ('OTHER',              'ANY',      NULL,
     'Other (state the reason)',                            true, true, 'U-ADM');

-- ================================================ PR-STANDARD v1 (definition)
-- Inserted as DRAFT, populated, and only THEN activated -- the same order the
-- API takes (`POST /definitions` creates a DRAFT, `POST /{id}/activate` flips
-- it). Inserting it ACTIVE up front would work, because the immutability
-- triggers guard UPDATE and DELETE rather than INSERT, but it would model a
-- lifecycle the product does not have and would leave this file silently
-- depending on that distinction.
INSERT INTO approval_definition
    (definition_id, object_type, code, version, status, entity_id,
     effective_from, effective_to, created_by)
VALUES
    ('APD-PR-STD-V1', 'PURCHASE_REQUEST', 'PR-STANDARD', 1, 'DRAFT',
     'ENT-DM1', '2026-04-01', NULL, 'U-ADM');

-- ------------------------------------------------------------------- rules
-- Evaluated in `priority` order; first match wins.
--
-- Priority 10 is the exception route: a requisition above Rs 50,00,000
-- (500000000 paise) or one already flagged as exceeding available budget.
-- Priority 100 is the catch-all, and it exists so that "no rule matched" means
-- a genuine configuration defect rather than an ordinary small requisition --
-- an unmatched object becomes EXCEPTION_PENDING with
-- APPROVAL_ROUTE_UNRESOLVED, never an approval (Contract 2).
INSERT INTO approval_rule
    (rule_id, definition_id, priority, predicate, description, created_by)
VALUES
    ('APR-PR-STD-V1-EXC', 'APD-PR-STD-V1', 10,
     '{"all": [
         {"field": "object_type",   "op": "eq", "value": "PURCHASE_REQUEST"},
         {"any": [
             {"field": "amount_paise",     "op": "gt", "value": 500000000},
             {"field": "exceeds_available", "op": "eq", "value": true}
         ]}
     ], "route": "EXCEPTION"}'::jsonb,
     'High-value or over-budget requisition: adds the Finance exception gate.',
     'U-ADM'),
    ('APR-PR-STD-V1-STD', 'APD-PR-STD-V1', 100,
     '{"all": [
         {"field": "object_type", "op": "eq", "value": "PURCHASE_REQUEST"}
     ], "route": "STANDARD"}'::jsonb,
     'Catch-all for every other purchase requisition in this entity.',
     'U-ADM');

-- ------------------------------------------------------------------ stages
-- Stages 2 and 3 share `parallel_group` PG-OPS: they open together and both
-- must resolve before stage 4 opens. Ordering them 2-then-3 sequentially would
-- make the Plant Head wait on Procurement for no reason -- these two reviews
-- are independent, and modelling them as a group is what stops the workflow
-- from inventing a dependency the business does not have.
--
-- Stage 4 is N_OF_M with quorum_n = 2: any two of the CAPEX Committee. The
-- database refuses an incoherent pair here (`ck_approval_stage_quorum`), so a
-- committee stage cannot be stored with, say, quorum_type ALL and a stray
-- quorum_n beside it that nobody reads.
--
-- Stage 5 is the exception gate: `applies_when` false for an in-budget
-- requisition, in which case it is still recorded, as SKIPPED with a
-- skip_reason. `requires_reason` is true because approving spend above
-- available budget is exactly the decision that must be justified in words,
-- and `reason_code_set` narrows the pick-list to the codes that fit.
INSERT INTO approval_stage
    (stage_id, definition_id, stage_no, name, parallel_group,
     quorum_type, quorum_n, applies_when, sla_hours, escalate_after_hours,
     escalate_to, allow_delegation, requires_reason, reason_code_set,
     created_by)
VALUES
    ('APS-PR-STD-V1-1', 'APD-PR-STD-V1', 1, 'Project Manager review', NULL,
     'ALL', NULL, NULL, 24, NULL, NULL, true, false, NULL, 'U-ADM'),
    ('APS-PR-STD-V1-2', 'APD-PR-STD-V1', 2, 'Plant Head sign-off', 'PG-OPS',
     'ALL', NULL, NULL, 48, NULL, NULL, true, false, NULL, 'U-ADM'),
    ('APS-PR-STD-V1-3', 'APD-PR-STD-V1', 3, 'Procurement review', 'PG-OPS',
     'ALL', NULL, NULL, 48, NULL, NULL, true, false, NULL, 'U-ADM'),
    ('APS-PR-STD-V1-4', 'APD-PR-STD-V1', 4, 'CAPEX Committee (any two)', NULL,
     'N_OF_M', 2, NULL, 72, 96,
     '{"kind": "ROLE", "ref": "CFO"}'::jsonb,
     true, false, NULL, 'U-ADM'),
    ('APS-PR-STD-V1-5', 'APD-PR-STD-V1', 5, 'Finance exception approval', NULL,
     'ALL', NULL,
     '{"any": [
         {"field": "amount_paise",      "op": "gt", "value": 500000000},
         {"field": "exceeds_available", "op": "eq", "value": true}
     ]}'::jsonb,
     24, 48,
     '{"kind": "ROLE", "ref": "CFO"}'::jsonb,
     -- Delegation is switched OFF for the exception gate: an over-budget
     -- approval is the one decision the demo insists a named Finance approver
     -- takes personally.
     false, true, 'BUDGET_EXCEPTION', 'U-ADM');

-- -------------------------------------------------------- approver slots
-- ROLE slots resolve through role_grant at routing time; the one USER slot
-- (stage 5) names an individual directly, so the demo exercises both kinds.
-- `scope_expr` narrows a role to the requisition's own project or plant --
-- without it, "Plant Head" would mean every Plant Head in the estate.
--
-- Whoever these resolve to, the engine then subtracts contributor_set(object)
-- -- maker, editors, prior actors (Contract 5). An empty set after that filter
-- is EXCEPTION_PENDING with NO_INDEPENDENT_APPROVER, not an approval.
INSERT INTO approval_stage_approver
    (stage_id, ordinal, approver_kind, approver_ref, scope_expr)
VALUES
    ('APS-PR-STD-V1-1', 1, 'ROLE', 'Project Manager',
     '{"match": "object.project_id"}'::jsonb),
    ('APS-PR-STD-V1-2', 1, 'ROLE', 'Plant Head',
     '{"match": "object.plant_id"}'::jsonb),
    ('APS-PR-STD-V1-3', 1, 'ROLE', 'Procurement', NULL),
    -- Three committee slots, quorum 2: the M of the N_OF_M is the resolved
    -- assignee count, so a committee of three approving by any two is exactly
    -- what stage 4's quorum_n = 2 means.
    ('APS-PR-STD-V1-4', 1, 'ROLE', 'CAPEX Committee', NULL),
    ('APS-PR-STD-V1-4', 2, 'ROLE', 'Project Finance Controller', NULL),
    ('APS-PR-STD-V1-4', 3, 'ROLE', 'CFO', NULL),
    ('APS-PR-STD-V1-5', 1, 'ROLE', 'Finance',
     '{"match": "object.entity_id"}'::jsonb),
    ('APS-PR-STD-V1-5', 2, 'USER', 'U-PFC', NULL);

-- ------------------------------------------------------------- activate v1
-- DRAFT -> ACTIVE. From this statement onward the definition and every rule,
-- stage and approver slot above are immutable: the triggers in 008 refuse both
-- UPDATE and DELETE on all four tables. Changing this workflow means creating
-- v2, which is exactly what the next block does.
UPDATE approval_definition
   SET status       = 'ACTIVE',
       activated_at = '2026-04-01T00:00:00+00:00',
       activated_by = 'U-ADM'
 WHERE definition_id = 'APD-PR-STD-V1';

-- ============================================== PR-STANDARD v2 (DRAFT)
-- The successor, left editable on purpose. It is a real, separate row -- the
-- UNIQUE (object_type, code, version) constraint is what makes "PR-STANDARD
-- v2" a nameable thing distinct from v1 rather than a mutation of it -- and it
-- is NOT in effect: v1 stays the definition anything routes under until an
-- administrator activates v2 and retires v1.
--
-- Its difference from v1 is deliberately small and legible: the committee
-- quorum rises from any-two to any-three. That is precisely the kind of change
-- that must never be applied in place, because every requisition already
-- approved by two committee members was correctly approved under v1 and would
-- read as under-approved if v1's own row were edited.
INSERT INTO approval_definition
    (definition_id, object_type, code, version, status, entity_id,
     effective_from, effective_to, created_by)
VALUES
    ('APD-PR-STD-V2', 'PURCHASE_REQUEST', 'PR-STANDARD', 2, 'DRAFT',
     'ENT-DM1', '2027-04-01', NULL, 'U-ADM');

INSERT INTO approval_rule
    (rule_id, definition_id, priority, predicate, description, created_by)
VALUES
    ('APR-PR-STD-V2-STD', 'APD-PR-STD-V2', 100,
     '{"all": [
         {"field": "object_type", "op": "eq", "value": "PURCHASE_REQUEST"}
     ], "route": "STANDARD"}'::jsonb,
     'Catch-all, unchanged from v1.', 'U-ADM');

INSERT INTO approval_stage
    (stage_id, definition_id, stage_no, name, parallel_group,
     quorum_type, quorum_n, applies_when, sla_hours, escalate_after_hours,
     escalate_to, allow_delegation, requires_reason, reason_code_set,
     created_by)
VALUES
    ('APS-PR-STD-V2-1', 'APD-PR-STD-V2', 1, 'Project Manager review', NULL,
     'ALL', NULL, NULL, 24, NULL, NULL, true, false, NULL, 'U-ADM'),
    ('APS-PR-STD-V2-2', 'APD-PR-STD-V2', 2, 'CAPEX Committee (any three)', NULL,
     'N_OF_M', 3, NULL, 72, 96,
     '{"kind": "ROLE", "ref": "CFO"}'::jsonb,
     true, false, NULL, 'U-ADM');

INSERT INTO approval_stage_approver
    (stage_id, ordinal, approver_kind, approver_ref, scope_expr)
VALUES
    ('APS-PR-STD-V2-1', 1, 'ROLE', 'Project Manager',
     '{"match": "object.project_id"}'::jsonb),
    ('APS-PR-STD-V2-2', 1, 'ROLE', 'CAPEX Committee', NULL),
    ('APS-PR-STD-V2-2', 2, 'ROLE', 'Project Finance Controller', NULL),
    ('APS-PR-STD-V2-2', 3, 'ROLE', 'CFO', NULL);

-- =============================== a second entity, so scope is demonstrable
-- ENT-DM2 gets its own, deliberately different workflow: a single Finance
-- gate, no committee. Two entities with two workflows is what makes the RLS
-- policy on approval_definition observable -- U-PFC (restricted to ENT-DM1)
-- and U-FIN (restricted to ENT-DM2) must each see exactly one of these, and
-- neither may enumerate the other's thresholds or named approvers.
INSERT INTO approval_definition
    (definition_id, object_type, code, version, status, entity_id,
     effective_from, effective_to, created_by)
VALUES
    ('APD-PR-DM2-V1', 'PURCHASE_REQUEST', 'PR-SOLAR', 1, 'DRAFT',
     'ENT-DM2', '2026-04-01', NULL, 'U-ADM');

INSERT INTO approval_rule
    (rule_id, definition_id, priority, predicate, description, created_by)
VALUES
    ('APR-PR-DM2-V1-STD', 'APD-PR-DM2-V1', 100,
     '{"all": [
         {"field": "object_type", "op": "eq", "value": "PURCHASE_REQUEST"}
     ], "route": "STANDARD"}'::jsonb,
     'Every solar-park requisition takes the single Finance gate.', 'U-ADM');

INSERT INTO approval_stage
    (stage_id, definition_id, stage_no, name, parallel_group,
     quorum_type, quorum_n, applies_when, sla_hours, escalate_after_hours,
     escalate_to, allow_delegation, requires_reason, reason_code_set,
     created_by)
VALUES
    ('APS-PR-DM2-V1-1', 'APD-PR-DM2-V1', 1, 'Finance approval', NULL,
     'ANY', NULL, NULL, 48, NULL, NULL, true, false, NULL, 'U-ADM');

INSERT INTO approval_stage_approver
    (stage_id, ordinal, approver_kind, approver_ref, scope_expr)
VALUES
    ('APS-PR-DM2-V1-1', 1, 'ROLE', 'Finance',
     '{"match": "object.entity_id"}'::jsonb);

UPDATE approval_definition
   SET status       = 'ACTIVE',
       activated_at = '2026-04-01T00:00:00+00:00',
       activated_by = 'U-ADM'
 WHERE definition_id = 'APD-PR-DM2-V1';

-- ========================================== an organisation-wide definition
-- `entity_id` NULL: this workflow applies everywhere. It is here because the
-- NULL case is a real branch of the RLS policy -- a NULL entity_id waives the
-- entity dimension, so EVERY principal sees this definition, including U-PFC
-- and U-FIN who each see only one of the two entity-specific ones above. A
-- demo without this row would leave that branch untested against real data.
INSERT INTO approval_definition
    (definition_id, object_type, code, version, status, entity_id,
     effective_from, effective_to, created_by)
VALUES
    ('APD-BREV-ORG-V1', 'BUDGET_REVISION', 'BREV-ORGWIDE', 1, 'DRAFT',
     NULL, '2026-04-01', NULL, 'U-ADM');

INSERT INTO approval_rule
    (rule_id, definition_id, priority, predicate, description, created_by)
VALUES
    ('APR-BREV-ORG-V1-STD', 'APD-BREV-ORG-V1', 100,
     '{"all": [
         {"field": "object_type", "op": "eq", "value": "BUDGET_REVISION"}
     ], "route": "STANDARD"}'::jsonb,
     'Every budget revision, in every entity.', 'U-ADM');

INSERT INTO approval_stage
    (stage_id, definition_id, stage_no, name, parallel_group,
     quorum_type, quorum_n, applies_when, sla_hours, escalate_after_hours,
     escalate_to, allow_delegation, requires_reason, reason_code_set,
     created_by)
VALUES
    ('APS-BREV-ORG-V1-1', 'APD-BREV-ORG-V1', 1,
     'Project Finance Controller review', NULL,
     'ALL', NULL, NULL, 24, NULL, NULL, true, true, 'BUDGET_EXCEPTION',
     'U-ADM');

INSERT INTO approval_stage_approver
    (stage_id, ordinal, approver_kind, approver_ref, scope_expr)
VALUES
    ('APS-BREV-ORG-V1-1', 1, 'ROLE', 'Project Finance Controller', NULL);

UPDATE approval_definition
   SET status       = 'ACTIVE',
       activated_at = '2026-04-01T00:00:00+00:00',
       activated_by = 'U-ADM'
 WHERE definition_id = 'APD-BREV-ORG-V1';

-- =============================================================== delegation
-- One ACTIVE delegation (revoked_at IS NULL, and the range spans the demo's
-- OPEN period P-DM1-Q2, 2026-07-01..2026-09-30): the CFO delegates purchase
-- requisition authority in ENT-DM1 to Finance for a planned absence.
--
-- `active_range` is a daterange with an EXCLUSIVE upper bound, which is why
-- the end reads 2026-10-01 for a delegation whose last effective day is
-- 30 September. `ck_approval_delegation_range_not_empty` rejects an empty
-- range, so the classic `[d, d)` typo -- a "one-day" delegation that
-- authorises nothing -- cannot be stored looking like a grant.
--
-- Delegating does NOT launder a self-approval. Contract 5 refuses a decision
-- if EITHER the actor OR the acting-for identity is a contributor to the
-- document, and both are recorded on every approval_action row so the check is
-- auditable after the fact.
INSERT INTO approval_delegation
    (delegation_id, delegator_user_id, delegate_user_id, scope_key,
     active_range, created_by, revoked_at, revoke_reason)
VALUES
    ('ADG-CFO-FIN-Q2', 'U-CFO', 'U-FIN', 'PURCHASE_REQUEST:ENT-DM1',
     daterange('2026-07-01', '2026-10-01', '[)'), 'U-ADM', NULL, NULL);

-- A second, already-REVOKED delegation. Revocation is a stamp with a mandatory
-- reason, never a DELETE: erasing the row would erase the fact that authority
-- was ever transferred, which is the fact an auditor needs when reviewing a
-- decision taken while it was live.
INSERT INTO approval_delegation
    (delegation_id, delegator_user_id, delegate_user_id, scope_key,
     active_range, created_by, revoked_at, revoke_reason)
VALUES
    ('ADG-PLH-PM-REVOKED', 'U-PLH', 'U-PM', 'PURCHASE_REQUEST:ENT-DM1',
     daterange('2026-05-01', '2026-06-01', '[)'), 'U-ADM',
     '2026-05-15T09:30:00+00:00', 'Returned from leave early.');
