-- 019_closure.sql
-- Project closure, capitalisation and asset allocation: the three tables the
-- SQLite build has had since `app/backend/db.py` and PostgreSQL has never had.
--
-- WHAT THIS CLOSES
--
-- `app/backend/services.py::approve_capitalisation` is the only implementation
-- of the capitalisation gate in this codebase, and it runs against SQLite.
-- `capitalisation_request` and `asset_allocation` are created by
-- `app/backend/db.py` and by NO PostgreSQL migration, so the whole closure
-- chain -- completion review, CWIP allocation, capitalisation decision -- was
-- unreachable on the schema the application is being migrated onto. This
-- migration creates it there, keyed and constrained the way migrations 013 and
-- 014 key their documents.
--
--
-- EXCLUSION X-01 IS ENFORCED BY A CHECK CONSTRAINT, NOT BY A COMMENT.
--
-- This system records the DECISION to capitalise. It performs no general
-- ledger posting and no fixed-asset register write, because no such
-- integration exists in this build and none is in scope. Every prior surface
-- says so in prose -- `services.approve_capitalisation` returns
-- `posting_status = "NOT POSTED - local approval only; no ERP/GL posting
-- exists"`, `main.py::capitalisation` stamps the same string on every row --
-- and prose is exactly what gets edited by someone who later wires a posting
-- and forgets one of the two places.
--
--     ck_capitalisation_request_not_posted CHECK (posting_status = 'NOT POSTED')
--
-- makes the honesty structural. A future migration that genuinely adds a
-- posting must DROP this constraint, in its own file, under review, which is
-- the point: the claim "this was posted" cannot be made by an UPDATE that
-- nobody noticed. The column exists at all so that the claim is STORED rather
-- than synthesised by whichever layer happens to be rendering.
--
--
-- WHY A COMPLETION REVIEW IS ITS OWN TABLE
--
-- SCR-20 (Project Completion Review) and SCR-21 (Capitalisation Workbench) are
-- two decisions by two different people at two different times: technical
-- completion is asserted by the project side, capitalisation is approved by
-- the CAPEX committee. Folding the first into `capitalisation_request` would
-- make the second's maker-checker meaningless -- there would be one row, one
-- `requested_by`, and no independent assertion of completion to check against.
--
-- `ux_project_completion_review_live` keeps ONE live review per project, so a
-- project cannot accumulate competing completion assertions. It is PARTIAL on
-- the open states: a rejected review is history and must not block the next
-- attempt.
--
--
-- MONEY
--
-- `cwip_balance_paise`, `allocated_paise` and `amount_paise` are `bigint`
-- integer paise, like every other money column in this schema.
-- `allocated_paise` on the request is a DERIVED CACHE of
-- `SUM(asset_allocation.amount_paise)` and is recomputed by `pg/closure.py`
-- inside the same transaction as every allocation write -- it is never the
-- authority. The authority is the child rows, which is why there is no CHECK
-- tying the two: a constraint over a sum would need a trigger, and a trigger
-- that can be out of date is worse than a cache documented as one.
--
-- `amount_paise` on an allocation is `> 0`. A write-off is a POSITIVE amount
-- carrying `is_writeoff = true` and a reason, never a negative allocation: the
-- write-off has to be visible AS a write-off, and a sign is not a category.
-- `ck_asset_allocation_writeoff_reason` makes the reason mandatory the moment
-- the flag is set.
--
--
-- SCOPE
--
-- All three tables carry a denormalised `project_id` bound by foreign key to a
-- real project, and all three reach ENTITY, PLANT, LOCATION and PROJECT through
-- it -- the `purchase_request` shape from 013, for the reason 013's header and
-- `rls.RLS_PROCUREMENT_TABLE_COLUMNS` both give: dimensions resolve
-- independently, so a policy filtering `project_id` alone hands a caller
-- restricted to one entity (and to no project) every other entity's CWIP
-- balance.
--
-- `asset_allocation.wbs_id` is additionally bound to the SAME project by
-- `fk_asset_allocation_wbs_project`, against `wbs_element (wbs_id, project_id)`
-- -- the composite unique 002 created for exactly this. Without it an
-- allocation could name another project's WBS element and the policy would
-- still admit the row, because the policy reads `project_id`.

BEGIN;

-- ================================================ project_completion_review
-- SCR-20. The technical assertion that a project has finished, and the
-- decision on that assertion. It is NOT the capitalisation decision.
CREATE TABLE project_completion_review (
    review_id      text PRIMARY KEY,
    project_id     text NOT NULL REFERENCES project (project_id),

    status         text NOT NULL DEFAULT 'Draft',

    -- The date the project side asserts physical/technical completion. Not
    -- derived from any document: nothing in this schema knows when the last
    -- bolt was tightened, and inferring it from the newest GRN would be a
    -- fabricated fact wearing a date.
    completion_date date,

    summary        text,
    decision_note  text,

    submitted_by   text,
    submitted_at   timestamptz,
    decided_by     text,
    decided_at     timestamptz,

    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,

    CONSTRAINT ck_project_completion_review_status CHECK (
        status IN ('Draft', 'Submitted', 'Accepted', 'Rejected')
    ),

    -- A submitted review names WHO submitted it and WHEN, or it is not
    -- submitted. Same shape as `ck_reconciliation_exception_resolution` in 011,
    -- and for the same reason: a state that arrived without an actor is
    -- indistinguishable from one that was never asserted.
    CONSTRAINT ck_project_completion_review_submission CHECK (
        (status = 'Draft'
             AND submitted_at IS NULL AND submitted_by IS NULL)
        OR
        (status <> 'Draft'
             AND submitted_at IS NOT NULL AND submitted_by IS NOT NULL)
    ),

    -- A decided review names its decider, its time and its reason.
    CONSTRAINT ck_project_completion_review_decision CHECK (
        (status IN ('Draft', 'Submitted')
             AND decided_at IS NULL AND decided_by IS NULL
             AND decision_note IS NULL)
        OR
        (status IN ('Accepted', 'Rejected')
             AND decided_at IS NOT NULL AND decided_by IS NOT NULL
             AND decision_note IS NOT NULL AND btrim(decision_note) <> '')
    ),

    -- A review that leaves Draft asserts a completion date.
    CONSTRAINT ck_project_completion_review_completion_date CHECK (
        status = 'Draft' OR completion_date IS NOT NULL
    ),

    -- The FK target for `capitalisation_request.review_id`, which must not be
    -- able to cite a review belonging to another project.
    CONSTRAINT ux_project_completion_review_project
        UNIQUE (review_id, project_id)
);

CREATE INDEX ix_project_completion_review_project
    ON project_completion_review (project_id);

-- ONE live review per project. Partial on the two open states so a rejected
-- review is history rather than a permanent block on the next attempt.
CREATE UNIQUE INDEX ux_project_completion_review_live
    ON project_completion_review (project_id)
    WHERE status IN ('Draft', 'Submitted');

-- ==================================================== capitalisation_request
-- SCR-21. The capitalisation DECISION, and nothing beyond it. See the header
-- on `ck_capitalisation_request_not_posted`.
CREATE TABLE capitalisation_request (
    cap_id         text PRIMARY KEY,
    cap_number     text NOT NULL,
    project_id     text NOT NULL REFERENCES project (project_id),

    -- Nullable: a request can be raised before the review is accepted, and the
    -- approval gate in `pg/closure.py` is what refuses to approve without one.
    -- Bound to the SAME project by the composite FK below, so a request can
    -- never cite another project's completion review.
    review_id      text,

    status         text NOT NULL DEFAULT 'Draft',

    -- The CWIP balance this request was raised against, captured at submission
    -- so the figure the approver saw is the figure recorded. Recomputed and
    -- re-checked at approval; a divergence is a refusal, not a silent update.
cwip_balance_paise bigint NOT NULL DEFAULT 0,

    -- Derived cache of SUM(asset_allocation.amount_paise). See the header.
allocated_paise bigint NOT NULL DEFAULT 0,

    -- X-01. Structural, not prose. A future posting integration must drop
    -- `ck_capitalisation_request_not_posted` in its own migration.
    posting_status text NOT NULL DEFAULT 'NOT POSTED',

    requested_by   text NOT NULL,
    requested_at   timestamptz NOT NULL DEFAULT now(),
    submitted_at   timestamptz,
    approver       text,
    approved_at    timestamptz,
    decision_note  text,

    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,

    CONSTRAINT ux_capitalisation_request_number UNIQUE (cap_number),

    -- The FK target for `asset_allocation`'s composite, so an allocation can
    -- never be hung off a request belonging to a different project.
    CONSTRAINT ux_capitalisation_request_project UNIQUE (cap_id, project_id),

    CONSTRAINT fk_capitalisation_request_review_project
        FOREIGN KEY (review_id, project_id)
        REFERENCES project_completion_review (review_id, project_id)
        ON UPDATE RESTRICT,

    CONSTRAINT ck_capitalisation_request_status CHECK (
        status IN ('Draft', 'Submitted', 'Approved', 'Rejected')
    ),

    -- THE EXCLUSION, ENFORCED. Read the header before changing this line.
    CONSTRAINT ck_capitalisation_request_not_posted CHECK (
        posting_status = 'NOT POSTED'
    ),

    CONSTRAINT ck_capitalisation_request_money_nonneg CHECK (
        cwip_balance_paise >= 0 AND allocated_paise >= 0
    ),

    -- A decided request names its approver, its time and its reason.
    CONSTRAINT ck_capitalisation_request_decision CHECK (
        (status IN ('Draft', 'Submitted')
             AND approved_at IS NULL AND approver IS NULL)
        OR
        (status IN ('Approved', 'Rejected')
             AND approved_at IS NOT NULL AND approver IS NOT NULL
             AND decision_note IS NOT NULL AND btrim(decision_note) <> '')
    ),

    CONSTRAINT ck_capitalisation_request_submission CHECK (
        status = 'Draft' OR submitted_at IS NOT NULL
    )
);

CREATE INDEX ix_capitalisation_request_project
    ON capitalisation_request (project_id);

-- ONE live capitalisation request per project, for the reason the completion
-- review has one: two open requests against one CWIP balance would each pass
-- the allocation-equals-balance check and capitalise the same rupees twice.
CREATE UNIQUE INDEX ux_capitalisation_request_live
    ON capitalisation_request (project_id)
    WHERE status IN ('Draft', 'Submitted');

-- ========================================================= asset_allocation
-- SCR-22. How a CWIP balance is split into the assets it becomes -- and what
-- part of it is written off instead.
CREATE TABLE asset_allocation (
    allocation_id  text PRIMARY KEY,
    cap_id         text NOT NULL REFERENCES capitalisation_request (cap_id),

    -- Denormalised, bound to the request's project by
    -- `fk_asset_allocation_cap_project` and to a real WBS element of THAT
    -- project by `fk_asset_allocation_wbs_project`.
    project_id     text NOT NULL,
    wbs_id         text NOT NULL,

    asset_name     text NOT NULL,
    asset_category text,

    -- Always positive. A write-off is a positive amount with the flag set, not
    -- a negative allocation. See the header.
amount_paise   bigint NOT NULL,

    is_writeoff    boolean NOT NULL DEFAULT false,
    writeoff_reason text,

    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,

    CONSTRAINT fk_asset_allocation_cap_project
        FOREIGN KEY (cap_id, project_id)
        REFERENCES capitalisation_request (cap_id, project_id)
        ON UPDATE RESTRICT,
    CONSTRAINT fk_asset_allocation_wbs_project
        FOREIGN KEY (wbs_id, project_id)
        REFERENCES wbs_element (wbs_id, project_id) ON UPDATE RESTRICT,

    CONSTRAINT ck_asset_allocation_amount_positive CHECK (amount_paise > 0),

    CONSTRAINT ck_asset_allocation_writeoff_reason CHECK (
        is_writeoff = false
        OR (writeoff_reason IS NOT NULL AND btrim(writeoff_reason) <> '')
    ),

    CONSTRAINT ck_asset_allocation_name_present CHECK (btrim(asset_name) <> '')
);

CREATE INDEX ix_asset_allocation_cap ON asset_allocation (cap_id);
CREATE INDEX ix_asset_allocation_wbs ON asset_allocation (wbs_id);

-- ========================================================= row-level security
-- The 013 shape: reach `project` and pass ALL FOUR of its dimension columns.
-- Filtering this table's own `project_id` alone would waive entity, plant and
-- location, and a principal restricted to one entity but to no project carries
-- `project_ids = NULL` -- unrestricted -- so such a predicate returns TRUE for
-- every project in the estate. See `rls.RLS_PROCUREMENT_TABLE_COLUMNS`.
--
-- ARGUMENT ORDER: capex_scope_permits(entity, plant, location, project). All
-- four parameters are `text`, so PostgreSQL accepts any order silently; 011's
-- header records the review that caught exactly that mistake.
ALTER TABLE project_completion_review ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_completion_review FORCE ROW LEVEL SECURITY;
CREATE POLICY project_completion_review_scope ON project_completion_review
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = project_completion_review.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = project_completion_review.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

ALTER TABLE capitalisation_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE capitalisation_request FORCE ROW LEVEL SECURITY;
CREATE POLICY capitalisation_request_scope ON capitalisation_request
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = capitalisation_request.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = capitalisation_request.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

ALTER TABLE asset_allocation ENABLE ROW LEVEL SECURITY;
ALTER TABLE asset_allocation FORCE ROW LEVEL SECURITY;
CREATE POLICY asset_allocation_scope ON asset_allocation
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = asset_allocation.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = asset_allocation.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

-- ================================================== privileges for capex_app
-- Restated rather than relied upon, exactly as 011, 014 and 015 state theirs:
-- 004's ALTER DEFAULT PRIVILEGES attaches to the ROLE THAT ISSUED IT, so a
-- deployment whose 018 is applied by a different identity than its 004 would
-- otherwise be left guessing.
GRANT SELECT, INSERT, UPDATE ON project_completion_review TO capex_app;
GRANT SELECT, INSERT, UPDATE ON capitalisation_request TO capex_app;
GRANT SELECT, INSERT, UPDATE ON asset_allocation TO capex_app;

-- And the REVOKE that makes the record true. A capitalisation decision, a
-- completion review and an asset allocation are all financial records: they
-- are superseded by a new row or a status change, never deleted. A DELETE
-- privilege would make "this project was never reviewed" indistinguishable
-- from "this project's review was removed".
REVOKE DELETE ON project_completion_review FROM capex_app;
REVOKE DELETE ON capitalisation_request FROM capex_app;
REVOKE DELETE ON asset_allocation FROM capex_app;

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- Order matters: asset_allocation references capitalisation_request,
--   -- which references project_completion_review.
--   DROP POLICY IF EXISTS asset_allocation_scope ON asset_allocation;
--   DROP POLICY IF EXISTS capitalisation_request_scope ON capitalisation_request;
--   DROP POLICY IF EXISTS project_completion_review_scope ON project_completion_review;
--   DROP TABLE IF EXISTS asset_allocation;
--   DROP TABLE IF EXISTS capitalisation_request;
--   DROP TABLE IF EXISTS project_completion_review;
--   -- Every capitalisation DECISION recorded here is lost with the tables.
--   -- Nothing was ever posted to a general ledger from them -- see
--   -- ck_capitalisation_request_not_posted -- so no external system needs
--   -- reversing, but the local decision record does not exist anywhere else.
--   -- Export before running this.
--   COMMIT;
