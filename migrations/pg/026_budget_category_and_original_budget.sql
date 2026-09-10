-- 026_budget_category_and_original_budget.sql
-- Fable 5.1 corrective build: the ORIGINAL BUDGET document and the BUDGET
-- CATEGORY master, closing the gap between "budget_line kind='ORIGINAL' rows
-- exist and are immutable" (003) and the approved plan's U1 "Create budget",
-- REQ-WBS-001, REQ-BUD-020, REQ-REV-008 and the intake step "Upload or create
-- an original budget" (research/00_intake/coverage_matrix.json, step 4).
--
-- Before this migration the very first grant on a cell was, in the words of
-- app/backend/pg/budget.py::record_original, "a seed/migration concern": no
-- document, no numbering, no approval, no maker-checker, no UI. A product
-- that cannot create a budget through its own controls is a reporting tool
-- for budgets created somewhere else.
--
-- PRODUCT-OWNER DECISION RESOLVING AMB-04 (recorded in
-- docs/APPROVED_PRODUCTION_IMPLEMENTATION_PLAN.md, ambiguity register):
-- BUDGET CATEGORY and BUDGET HEAD are SEPARATE dimensions. Category is never
-- an alias of budget_head_id, never derived from asset_category or
-- item_category. Both are filterable, groupable and reportable
-- independently.
--
-- THE MODEL, stated once:
--   * The CONTROL CELL stays keyed by (wbs_id, budget_head_id). Every
--     availability question, every lock and every rollup is unchanged.
--   * A cell carries exactly ONE category, `budget_control_cell.
--     budget_category_id`, assigned when the original budget that creates
--     the cell is RELEASED, and immutable from then on (trigger below). Two
--     categories under one WBS x head would need two cells, which is a
--     different control grain and a different product; refused by design.
--   * Every `budget_line` (ORIGINAL, REVISION, TRANSFER leg) carries the
--     category of its cell, `budget_line.budget_category_id`, so a line-level
--     report can group by category without a join, and a trigger refuses a
--     line whose category disagrees with its cell.
--   * Reporting reads the CELL's category, so changing a category filter
--     narrows by classification while a budget-head filter narrows by
--     control dimension; tests/test_pg_original_budget.py proves the two
--     filters select different row sets over the same data.
--
-- THE DOCUMENT:
--   original_budget         the maker-checker document: entity, project,
--                           financial year, accounting period, title,
--                           justification, custom fields, approving
--                           authority, status lifecycle
--                           DRAFT -> SUBMITTED -> RELEASED | REJECTED |
--                           RETURNED -> DRAFT, or CANCELLED.
--   original_budget_line    one row per (wbs, head) the document grants,
--                           with the classification dimensions the intake
--                           asks for (division, branch, zone, plant,
--                           location) resolved and stored, the category, an
--                           integer-paise amount, justification and custom
--                           fields. On release each line writes exactly one
--                           kind='ORIGINAL' `budget_line` and records its id.
--
-- IMMUTABILITY AT THE DATABASE LEVEL, not discipline:
--   * a RELEASED original_budget row refuses UPDATE and DELETE outright;
--   * its lines refuse UPDATE and DELETE while the parent is RELEASED;
--   * the kind='ORIGINAL' `budget_line` rows they create were already
--     immutable under 003's trigger;
--   * a cell's category refuses UPDATE once set (NULL -> value is the only
--     transition, and only through the release path).
-- Supplements, reductions and transfers stay what 003 made them: separate
-- controlled records (`budget_revision`, `budget_transfer`).
--
-- NUMBERING: series `ORIGINAL_BUDGET`, prefix `OB-`, YEARLY reset, minted
-- through app/backend/pg/masters.py::issue_number -- the collision-safe
-- `ON CONFLICT ... RETURNING` path the plan's REQ-WBS-001 requires.
--
-- CUSTOM FIELDS (REQ-BUD-020): the 005 machinery is reused, not duplicated.
-- Its two CHECK constraints admitted only ITEM and VENDOR; they now also
-- admit BUDGET (applicability) and ORIGINAL_BUDGET (values). The document
-- additionally carries a `custom_fields` jsonb snapshot of the values as
-- entered, so an export shows what the approver saw even if a definition
-- is later deactivated.
--
-- Expand-only. Nothing 001..025 created is dropped or narrowed; the two
-- CHECK constraints are WIDENED (dropped and re-added with a superset).
--
-- ROLLBACK:
--   DROP TRIGGER IF EXISTS original_budget_released_immutable ON original_budget;
--   DROP TRIGGER IF EXISTS original_budget_line_released_immutable ON original_budget_line;
--   DROP TRIGGER IF EXISTS budget_control_cell_category_immutable ON budget_control_cell;
--   DROP TRIGGER IF EXISTS budget_line_category_matches_cell ON budget_line;
--   DROP FUNCTION IF EXISTS assert_original_budget_released_immutable() CASCADE;
--   DROP FUNCTION IF EXISTS assert_original_budget_line_released_immutable() CASCADE;
--   DROP FUNCTION IF EXISTS assert_cell_category_immutable() CASCADE;
--   DROP FUNCTION IF EXISTS assert_budget_line_category_matches_cell() CASCADE;
--   DROP TABLE IF EXISTS original_budget_line, original_budget CASCADE;
--   ALTER TABLE budget_line DROP COLUMN IF EXISTS budget_category_id;
--   ALTER TABLE budget_control_cell DROP COLUMN IF EXISTS budget_category_id;
--   DROP TABLE IF EXISTS budget_category CASCADE;
--   ALTER TABLE custom_field_applicability DROP CONSTRAINT IF EXISTS custom_field_applicability_applies_to_check;
--   ALTER TABLE custom_field_applicability ADD CONSTRAINT custom_field_applicability_applies_to_check CHECK (applies_to IN ('ITEM', 'VENDOR'));
--   ALTER TABLE custom_field_value DROP CONSTRAINT IF EXISTS custom_field_value_object_type_check;
--   ALTER TABLE custom_field_value ADD CONSTRAINT custom_field_value_object_type_check CHECK (object_type IN ('ITEM', 'VENDOR'));
--   DELETE FROM numbering_series WHERE code = 'ORIGINAL_BUDGET';
--   DELETE FROM schema_migrations WHERE version = '026';

-- ============================================================ budget_category
-- A genuine master, separate from budget_head. `entity_id` NULL means the
-- category applies estate-wide; a non-NULL value restricts it to one entity
-- (the "applicability by entity" the product owner asked for). Hierarchy is
-- optional through `parent_category_id`; a category may not be its own
-- parent, and deactivating a parent is refused by the service while an
-- active child exists (governed deactivation).
CREATE TABLE budget_category (
    category_id         text PRIMARY KEY,
    code                text NOT NULL,
    name                text NOT NULL,
    description         text,
    parent_category_id  text REFERENCES budget_category (category_id),
    display_order       integer NOT NULL DEFAULT 100,
    active              boolean NOT NULL DEFAULT true,
    entity_id           text REFERENCES entity (entity_id),
    effective_from      date,
    effective_to        date,
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text NOT NULL,
    version_no          integer NOT NULL DEFAULT 1,
    UNIQUE (code),
    CONSTRAINT ck_budget_category_not_own_parent
        CHECK (parent_category_id IS NULL OR parent_category_id <> category_id),
    CONSTRAINT ck_budget_category_effective_range
        CHECK (effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from),
    CONSTRAINT ck_budget_category_code_shape
        CHECK (code ~ '^[A-Z0-9][A-Z0-9_-]{1,39}$')
);

CREATE INDEX ix_budget_category_parent ON budget_category (parent_category_id);
CREATE INDEX ix_budget_category_entity ON budget_category (entity_id);

-- The cell's classification. NULL for every cell that pre-dates this
-- migration (the seed and the migrated POC estate); set exactly once, on
-- release of the original budget that grants the cell.
ALTER TABLE budget_control_cell
    ADD COLUMN budget_category_id text REFERENCES budget_category (category_id);
CREATE INDEX ix_control_cell_category ON budget_control_cell (budget_category_id);

-- Denormalised onto every grant line so line-level reporting groups by
-- category without a join; the trigger below keeps it equal to the cell's.
ALTER TABLE budget_line
    ADD COLUMN budget_category_id text REFERENCES budget_category (category_id);
CREATE INDEX ix_budget_line_category ON budget_line (budget_category_id);

CREATE OR REPLACE FUNCTION assert_cell_category_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.budget_category_id IS NOT NULL
       AND NEW.budget_category_id IS DISTINCT FROM OLD.budget_category_id THEN
        RAISE EXCEPTION
            'budget_control_cell %:% carries category % and it is immutable; changing it to % denied',
            OLD.wbs_id, OLD.budget_head_id, OLD.budget_category_id, NEW.budget_category_id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_control_cell_category_immutable BEFORE UPDATE ON budget_control_cell
    FOR EACH ROW EXECUTE FUNCTION assert_cell_category_immutable();

CREATE OR REPLACE FUNCTION assert_budget_line_category_matches_cell() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    cell_category text;
BEGIN
    IF NEW.budget_category_id IS NULL THEN
        RETURN NEW;                     -- pre-026 shape; the cell may be unclassified too
    END IF;
    SELECT budget_category_id INTO cell_category
      FROM budget_control_cell
     WHERE wbs_id = NEW.wbs_id AND budget_head_id = NEW.budget_head_id;
    IF cell_category IS NOT NULL AND cell_category <> NEW.budget_category_id THEN
        RAISE EXCEPTION
            'budget_line for cell %:% names category % but the cell is classified %; a cell has one category',
            NEW.wbs_id, NEW.budget_head_id, NEW.budget_category_id, cell_category
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER budget_line_category_matches_cell BEFORE INSERT OR UPDATE ON budget_line
    FOR EACH ROW EXECUTE FUNCTION assert_budget_line_category_matches_cell();

-- ============================================================ original_budget
CREATE TABLE original_budget (
    budget_id             text PRIMARY KEY,
    budget_number         text NOT NULL,
    entity_id             text NOT NULL REFERENCES entity (entity_id),
    project_id            text NOT NULL REFERENCES project (project_id),
    fiscal_year           text NOT NULL,
    period_id             text REFERENCES accounting_period (period_id),
    title                 text NOT NULL,
    justification         text,
    status                text NOT NULL DEFAULT 'DRAFT'
                          CHECK (status IN ('DRAFT', 'SUBMITTED', 'RELEASED',
                                            'REJECTED', 'RETURNED', 'CANCELLED')),
    -- The approving authority the intake asks to be CAPTURED (REQ-BUD-005):
    -- the user whose decision released the document, written by the
    -- approval write-back, never by the maker.
    approving_authority   text REFERENCES app_user (user_id),
    approval_instance_id  text,
    decision_note         text,
    submitted_at          timestamptz,
    submitted_by          text,
    decided_at            timestamptz,
    released_at           timestamptz,
    custom_fields         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            text NOT NULL REFERENCES app_user (user_id),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    updated_by            text NOT NULL,
    version_no            integer NOT NULL DEFAULT 1,
    UNIQUE (budget_number),
    CONSTRAINT ck_original_budget_fiscal_year_shape
        CHECK (fiscal_year ~ '^[0-9]{4}-[0-9]{2}$'),
    CONSTRAINT ck_original_budget_released_has_authority
        CHECK (status <> 'RELEASED' OR (approving_authority IS NOT NULL AND released_at IS NOT NULL))
);

CREATE INDEX ix_original_budget_project ON original_budget (project_id);
CREATE INDEX ix_original_budget_entity_status ON original_budget (entity_id, status);
CREATE INDEX ix_original_budget_created ON original_budget (created_at);

CREATE TABLE original_budget_line (
    line_id             text PRIMARY KEY,
    budget_id           text NOT NULL REFERENCES original_budget (budget_id),
    line_no             integer NOT NULL CHECK (line_no > 0),
    wbs_id              text NOT NULL REFERENCES wbs_element (wbs_id),
    budget_head_id      text NOT NULL REFERENCES budget_head (budget_head_id),
    budget_category_id  text NOT NULL REFERENCES budget_category (category_id),
    division_id         text REFERENCES division (division_id),
    branch_id           text REFERENCES branch (branch_id),
    zone_id             text REFERENCES zone (zone_id),
    plant_id            text REFERENCES plant (plant_id),
    location_id         text REFERENCES location (location_id),
    amount_paise        bigint NOT NULL CHECK (amount_paise > 0),
    justification       text,
    custom_fields       jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- Set on release: the kind='ORIGINAL' budget_line this line became.
    budget_line_id      text REFERENCES budget_line (budget_line_id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    created_by          text NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    updated_by          text NOT NULL,
    UNIQUE (budget_id, line_no),
    -- One grant per cell per document: two lines for the same WBS x head in
    -- one budget is a data-entry error, not a second budget.
    UNIQUE (budget_id, wbs_id, budget_head_id)
);

CREATE INDEX ix_original_budget_line_cell ON original_budget_line (wbs_id, budget_head_id);
CREATE INDEX ix_original_budget_line_category ON original_budget_line (budget_category_id);

CREATE OR REPLACE FUNCTION assert_original_budget_released_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'original_budget % is RELEASED and immutable: % denied',
        OLD.budget_id, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER original_budget_released_immutable BEFORE UPDATE OR DELETE ON original_budget
    FOR EACH ROW WHEN (OLD.status = 'RELEASED')
    EXECUTE FUNCTION assert_original_budget_released_immutable();

CREATE OR REPLACE FUNCTION assert_original_budget_line_released_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_status text;
BEGIN
    SELECT status INTO parent_status FROM original_budget WHERE budget_id = OLD.budget_id;
    IF parent_status = 'RELEASED' THEN
        RAISE EXCEPTION
            'original_budget_line % belongs to RELEASED budget % and is immutable: % denied',
            OLD.line_id, OLD.budget_id, TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER original_budget_line_released_immutable BEFORE UPDATE OR DELETE ON original_budget_line
    FOR EACH ROW EXECUTE FUNCTION assert_original_budget_line_released_immutable();

-- ============================================================ custom fields
ALTER TABLE custom_field_applicability
    DROP CONSTRAINT IF EXISTS custom_field_applicability_applies_to_check;
ALTER TABLE custom_field_applicability
    ADD CONSTRAINT custom_field_applicability_applies_to_check
    CHECK (applies_to IN ('ITEM', 'VENDOR', 'BUDGET'));
ALTER TABLE custom_field_value
    DROP CONSTRAINT IF EXISTS custom_field_value_object_type_check;
ALTER TABLE custom_field_value
    ADD CONSTRAINT custom_field_value_object_type_check
    CHECK (object_type IN ('ITEM', 'VENDOR', 'ORIGINAL_BUDGET'));

-- ============================================================ numbering
INSERT INTO numbering_series
    (series_id, code, prefix, suffix, pad_width, reset_policy, description,
     is_active, created_by, updated_by)
VALUES
    ('NS-ORIGINAL-BUDGET', 'ORIGINAL_BUDGET', 'OB-', '', 4, 'YEARLY',
     'Original budget documents; period_key is the fiscal year start, e.g. 2026',
     true, 'MIGRATION-026', 'MIGRATION-026')
ON CONFLICT (code) DO NOTHING;

-- ============================================================ grants
-- Same grants the earlier document tables carry for the application role:
-- read/insert/update; DELETE only where the service needs it (draft lines).
GRANT SELECT, INSERT, UPDATE ON budget_category TO capex_app;
GRANT SELECT, INSERT, UPDATE ON original_budget TO capex_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON original_budget_line TO capex_app;

-- ============================================================ RLS
-- budget_category: reference data with an OPTIONAL entity restriction. A
-- NULL entity_id row is estate-wide and visible to every established
-- principal; a non-NULL one is visible only inside that entity's scope.
ALTER TABLE budget_category ENABLE ROW LEVEL SECURITY;
ALTER TABLE budget_category FORCE ROW LEVEL SECURITY;
CREATE POLICY budget_category_scope ON budget_category
    USING      (capex_principal_present()
                AND (entity_id IS NULL OR capex_scope_permits(entity_id, NULL, NULL, NULL)))
    WITH CHECK (capex_principal_present()
                AND (entity_id IS NULL OR capex_scope_permits(entity_id, NULL, NULL, NULL)));

-- original_budget: entity_id and project_id are both NOT NULL, so the
-- direct predicate covers two dimensions and the project join supplies the
-- plant/location limbs exactly as `project`'s own policy does.
ALTER TABLE original_budget ENABLE ROW LEVEL SECURITY;
ALTER TABLE original_budget FORCE ROW LEVEL SECURITY;
CREATE POLICY original_budget_scope ON original_budget
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = original_budget.project_id
              AND capex_scope_permits(original_budget.entity_id, p.plant_id,
                                      p.location_id, original_budget.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = original_budget.project_id
              AND capex_scope_permits(original_budget.entity_id, p.plant_id,
                                      p.location_id, original_budget.project_id)
        )
    );

-- original_budget_line: reached through its parent document, which carries
-- the policy above. A line whose parent is invisible is invisible.
ALTER TABLE original_budget_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE original_budget_line FORCE ROW LEVEL SECURITY;
CREATE POLICY original_budget_line_scope ON original_budget_line
    USING (
        EXISTS (
            SELECT 1 FROM original_budget ob
            WHERE ob.budget_id = original_budget_line.budget_id
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM original_budget ob
            WHERE ob.budget_id = original_budget_line.budget_id
        )
    );

COMMENT ON TABLE budget_category IS
    'Budget CATEGORY master (Fable 5.1, AMB-04 resolved): a classification dimension SEPARATE from budget_head. One category per control cell, set on release, immutable.';
COMMENT ON TABLE original_budget IS
    'The original-budget document: maker-checker, numbered, approved through the engine, immutable once RELEASED. Its lines create kind=ORIGINAL budget_line rows.';
