-- 002_budget_control.sql
-- Milestone 1: budget control foundation.
--
-- Projects, the WBS hierarchy (ltree-backed), budget heads, and the budget
-- control cell -- the unit every availability check, lock and rollup in this
-- product is expressed in terms of: (wbs_id, budget_head_id). See
-- `.claude/skills/wbs-full-app-builder/references/domain-controls.md`,
-- section "The control cell", for the field list and rules this migration
-- exists to hold.
--
-- Two tables carry that cell, split by role rather than duplicated:
--   * budget_control_cell  the lock target and the single source of truth for
--                          "does this cell own budget" (budget_paise <> 0).
--                          `locking.lock_affected_cells` takes its
--                          `SELECT ... FOR UPDATE` against exactly this table.
--   * budget_ledger_cell   the rest of the per-cell financial position (own
--                          values only -- rollups are computed at read time
--                          by an ltree subtree scan, never stored here).
-- A budget_ledger_cell row's (wbs_id, budget_head_id) composite FK requires a
-- matching budget_control_cell row to exist first, so the two never drift
-- into referring to different cells.
--
-- No budget_line / PR / PO / bill tables yet -- those are Milestone 3+ once
-- the approval and procurement services land on top of this foundation. This
-- migration only needs to carry the fields those services will maintain.
--
-- Expand-only: nothing here alters or drops anything 001_foundation.sql
-- created.
--
-- ROLLBACK:
--   DROP TABLE IF EXISTS budget_ledger_cell, budget_control_cell, wbs_element,
--     budget_head, project CASCADE;

-- ---------------------------------------------------------------- project
CREATE TABLE project (
    project_id     text PRIMARY KEY,
    entity_id      text NOT NULL REFERENCES entity (entity_id),
    -- Plant and location are optional at the project level: a CAPEX project
    -- can span more than one, in which case the WBS elements underneath it
    -- carry their own, finer-grained location where it matters.
    plant_id       text REFERENCES plant (plant_id),
    location_id    text REFERENCES location (location_id),
    capex_code     text NOT NULL,
    name           text NOT NULL,
    status         text NOT NULL DEFAULT 'Draft',
    is_active      boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,
    UNIQUE (entity_id, capex_code)
);

CREATE INDEX ix_project_entity   ON project (entity_id);
CREATE INDEX ix_project_plant    ON project (plant_id) WHERE plant_id IS NOT NULL;
CREATE INDEX ix_project_location ON project (location_id) WHERE location_id IS NOT NULL;

-- ---------------------------------------------------------------- budget_head
-- A real control dimension (AUD-C-002 in domain.py): the ledger is keyed on
-- (wbs_id, budget_head_id), never pooled across heads under one WBS element.
CREATE TABLE budget_head (
    budget_head_id text PRIMARY KEY,
    entity_id      text NOT NULL REFERENCES entity (entity_id),
    code           text NOT NULL,
    name           text NOT NULL,
    active         boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,
    UNIQUE (entity_id, code)
);

CREATE INDEX ix_budget_head_entity ON budget_head (entity_id);
CREATE INDEX ix_budget_head_active ON budget_head (entity_id) WHERE active;

-- ---------------------------------------------------------------- wbs_element
-- `wbs_path` encodes the full ancestor chain as an ltree, root-first, and is
-- what `locking.lock_affected_cells` walks with `<@` to find every
-- ancestor-or-self of a spend. Labels must be ltree-safe (letters, digits,
-- underscore) -- generating a path-safe label from a `wbs_id` is the
-- inserting service's responsibility, not enforced here.
CREATE TABLE wbs_element (
    wbs_id          text PRIMARY KEY,
    project_id      text NOT NULL REFERENCES project (project_id),
    parent_wbs_id   text,
    wbs_code        text NOT NULL,
    description     text NOT NULL,
    wbs_path        ltree NOT NULL,
    level           integer NOT NULL DEFAULT 0,
    sort_order      integer NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'Draft',
    is_abandoned    boolean NOT NULL DEFAULT false,
    -- Convenience default only, ported from the SQLite prototype's
    -- `wbs_element.budget_head_id`. The real unit of budget control is always
    -- the (wbs_id, budget_head_id) cell in budget_control_cell below, never
    -- this column -- it exists so a procurement request that does not name a
    -- head explicitly has somewhere to fall back to.
    budget_head_id  text REFERENCES budget_head (budget_head_id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL,
    version_no      integer NOT NULL DEFAULT 1,
    UNIQUE (project_id, wbs_code),
    -- Required so the composite FK below has something to reference --
    -- PostgreSQL requires referenced columns to carry a unique constraint --
    -- and it states an invariant directly: a child's project_id can never
    -- diverge from its parent's.
    UNIQUE (wbs_id, project_id),
    CONSTRAINT fk_wbs_parent_same_project
        FOREIGN KEY (parent_wbs_id, project_id)
        REFERENCES wbs_element (wbs_id, project_id),
    CONSTRAINT ck_wbs_path_depth CHECK (nlevel(wbs_path) <= 100),
    CONSTRAINT ck_wbs_not_own_parent CHECK (parent_wbs_id IS DISTINCT FROM wbs_id)
);

CREATE INDEX ix_wbs_project   ON wbs_element (project_id);
CREATE INDEX ix_wbs_parent    ON wbs_element (parent_wbs_id) WHERE parent_wbs_id IS NOT NULL;
CREATE INDEX ix_wbs_path_gist ON wbs_element USING gist (wbs_path);
CREATE UNIQUE INDEX ux_wbs_path ON wbs_element (wbs_path);

-- ---------------------------------------------------------------- budget_control_cell
-- The lock target. `budget_paise` is the approved-AND-effective budget for
-- this cell; non-zero is exactly what makes a cell "budget-owning" for the
-- nearest-ancestor search and for `locking.lock_affected_cells`. A cell that
-- owns no budget carries no availability and is correctly excluded from
-- every lock and every rollup that matters.
CREATE TABLE budget_control_cell (
    wbs_id         text NOT NULL REFERENCES wbs_element (wbs_id),
    budget_head_id text NOT NULL REFERENCES budget_head (budget_head_id),
    budget_paise   bigint NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,
    PRIMARY KEY (wbs_id, budget_head_id),
    CONSTRAINT ck_control_cell_budget_nonneg CHECK (budget_paise >= 0)
);

CREATE INDEX ix_control_cell_head ON budget_control_cell (budget_head_id);
-- The lock scan filters on `budget_paise <> 0`; a partial index keeps that
-- cheap as a project's WBS tree grows, since most cells never own budget.
CREATE INDEX ix_control_cell_owning ON budget_control_cell (wbs_id, budget_head_id)
    WHERE budget_paise <> 0;

-- ---------------------------------------------------------------- budget_ledger_cell
-- The rest of the control cell's stored position -- own values only, per
-- domain-controls.md: rollups over the WBS subtree are computed at read time
-- by an ltree scan and are never persisted, because a persisted rollup will
-- drift. `original_paise` is immutable once written (kind='ORIGINAL' budget
-- lines only); `revisions_paise` and `actual_paise` are deliberately left
-- without a non-negative CHECK -- an approved cut or a reversal can carry
-- either negative, and that is correct, not a defect.
CREATE TABLE budget_ledger_cell (
    wbs_id                    text NOT NULL,
    budget_head_id            text NOT NULL,
    original_paise            bigint NOT NULL DEFAULT 0,
    revisions_paise           bigint NOT NULL DEFAULT 0,
    future_budget_paise       bigint NOT NULL DEFAULT 0,
    ordered_paise             bigint NOT NULL DEFAULT 0,
    commitment_paise          bigint NOT NULL DEFAULT 0,
    actual_paise              bigint NOT NULL DEFAULT 0,
    received_paise            bigint NOT NULL DEFAULT 0,
    received_not_billed_paise bigint NOT NULL DEFAULT 0,
    pr_reserved_paise         bigint NOT NULL DEFAULT 0,
    updated_at                timestamptz NOT NULL DEFAULT now(),
    updated_by                text NOT NULL,
    version_no                integer NOT NULL DEFAULT 1,
    PRIMARY KEY (wbs_id, budget_head_id),
    CONSTRAINT fk_ledger_control_cell
        FOREIGN KEY (wbs_id, budget_head_id)
        REFERENCES budget_control_cell (wbs_id, budget_head_id),
    CONSTRAINT ck_ledger_original_nonneg CHECK (original_paise >= 0),
    CONSTRAINT ck_ledger_future_nonneg CHECK (future_budget_paise >= 0),
    CONSTRAINT ck_ledger_ordered_nonneg CHECK (ordered_paise >= 0),
    CONSTRAINT ck_ledger_commitment_nonneg CHECK (commitment_paise >= 0),
    CONSTRAINT ck_ledger_received_nonneg CHECK (received_paise >= 0),
    CONSTRAINT ck_ledger_received_not_billed_nonneg CHECK (received_not_billed_paise >= 0),
    CONSTRAINT ck_ledger_pr_reserved_nonneg CHECK (pr_reserved_paise >= 0)
);
