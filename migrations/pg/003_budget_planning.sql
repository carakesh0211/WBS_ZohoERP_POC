-- 003_budget_planning.sql
-- Wave 2, stream 1 (Budget Control Backend): budget planning and revisions.
--
-- Adds the tables that let a budget actually CHANGE over time on top of the
-- Milestone 1 foundation (`budget_control_cell` / `budget_ledger_cell` in
-- 002_budget_control.sql, which this migration only reads and never alters):
--
--   * budget_line          one line of budget history per cell: the ORIGINAL
--                          grant, or a later REVISION / TRANSFER leg. This is
--                          the source of truth `budget_control_cell.budget_paise`
--                          and `budget_ledger_cell.{original,revisions,
--                          future_budget}_paise` are derived from -- see
--                          `app/backend/pg/budget.py::recompute_cell`.
--   * budget_revision      the maker-checker document for a single-cell
--                          change (a cut or an addition). Approving one
--                          writes exactly one REVISION-kind budget_line.
--   * budget_transfer      the maker-checker document for moving budget
--                          between two cells. Approving one writes a pair of
--                          TRANSFER-kind budget_line rows (a negative leg on
--                          the source cell, a positive leg on the
--                          destination), net zero.
--   * budget_version /
--     budget_version_cell  named point-in-time snapshots of every cell's own
--                          `budget_paise`, for the SCR-10 version-compare
--                          screen. A snapshot is data captured at the moment
--                          it is taken, not a live view -- comparing "the
--                          budget as it stood before this revision" to "the
--                          budget as it stands now" only means something if
--                          the earlier side is frozen.
--
-- kind='ORIGINAL' rows are immutable and undeletable AT THE DATABASE LEVEL
-- (see `.claude/skills/wbs-full-app-builder/references/domain-controls.md`,
-- "Budget-owning ancestor" / the frozen brief for this stream) -- a CHECK
-- constraint cannot see OLD, so this needs a trigger; REVISION and TRANSFER
-- rows are not protected by it and may be superseded (e.g. a transfer's legs
-- unwound by a later, offsetting transfer) as ordinary rows.
--
-- Expand-only: nothing here alters or drops anything 001_foundation.sql or
-- 002_budget_control.sql created.
--
-- ROLLBACK:
--   DROP TRIGGER IF EXISTS budget_line_original_immutable_update ON budget_line;
--   DROP TRIGGER IF EXISTS budget_line_original_immutable_delete ON budget_line;
--   DROP FUNCTION IF EXISTS assert_original_budget_line_immutable() CASCADE;
--   DROP TABLE IF EXISTS budget_version_cell, budget_version, budget_transfer,
--     budget_revision, budget_line CASCADE;

-- ---------------------------------------------------------------- budget_line
-- One row per grant of budget history. `amount_paise` is signed: positive for
-- an ORIGINAL grant or an increasing REVISION/TRANSFER-in leg, negative for a
-- cutting REVISION or a TRANSFER-out leg. A cell's current-and-effective
-- budget is SUM(amount_paise) over its Approved, currently-effective rows --
-- see `recompute_cell`. `effective_to` is open-ended (NULL) for everything
-- except a superseded/expired grant, which this milestone does not yet
-- create but the column exists for.
CREATE TABLE budget_line (
    budget_line_id  text PRIMARY KEY,
    wbs_id          text NOT NULL,
    budget_head_id  text NOT NULL,
    kind            text NOT NULL CHECK (kind IN ('ORIGINAL', 'REVISION', 'TRANSFER')),
    amount_paise    bigint NOT NULL,
    effective_from  date NOT NULL,
    effective_to    date,
    status          text NOT NULL DEFAULT 'Approved'
                    CHECK (status IN ('Draft', 'Submitted', 'Approved', 'Rejected', 'Cancelled')),
    justification   text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL,
    version_no      integer NOT NULL DEFAULT 1,
    CONSTRAINT fk_budget_line_control_cell
        FOREIGN KEY (wbs_id, budget_head_id)
        REFERENCES budget_control_cell (wbs_id, budget_head_id),
    -- An ORIGINAL grant must be a real, positive amount -- there is no such
    -- thing as a zero or negative "original" budget. A REVISION or TRANSFER
    -- leg must be non-zero (a zero-amount change carries no meaning and would
    -- silently no-op an approval while still leaving an audit trail implying
    -- something happened).
    CONSTRAINT ck_budget_line_amount CHECK (
        (kind = 'ORIGINAL' AND amount_paise > 0) OR
        (kind <> 'ORIGINAL' AND amount_paise <> 0)
    ),
    CONSTRAINT ck_budget_line_effective_range
        CHECK (effective_to IS NULL OR effective_to >= effective_from)
);

CREATE INDEX ix_budget_line_cell ON budget_line (wbs_id, budget_head_id);
CREATE INDEX ix_budget_line_status ON budget_line (status);
CREATE INDEX ix_budget_line_effective ON budget_line (effective_from);

-- kind='ORIGINAL' is immutable and undeletable at the database level -- a
-- CHECK constraint cannot see OLD, so this is a trigger. REVISION and
-- TRANSFER rows are NOT covered by the WHEN clause and may still be updated
-- (e.g. a Draft revision's own line before it is Approved never exists as a
-- row here in the first place -- see budget.py) or deleted.
CREATE OR REPLACE FUNCTION assert_original_budget_line_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'budget_line % is kind=ORIGINAL and is immutable: % denied',
        OLD.budget_line_id, TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER budget_line_original_immutable_update BEFORE UPDATE ON budget_line
    FOR EACH ROW WHEN (OLD.kind = 'ORIGINAL')
    EXECUTE FUNCTION assert_original_budget_line_immutable();
CREATE TRIGGER budget_line_original_immutable_delete BEFORE DELETE ON budget_line
    FOR EACH ROW WHEN (OLD.kind = 'ORIGINAL')
    EXECUTE FUNCTION assert_original_budget_line_immutable();

-- ------------------------------------------------------------ budget_revision
-- The maker-checker document for a single-cell budget change. Creating one
-- writes no budget_line and moves no money -- "a revision creates no spending
-- capacity until approved" (this stream's brief). Approving one writes
-- exactly one REVISION-kind budget_line and records it here in
-- `budget_line_id`, so the document and the money movement it caused are
-- always traceable to each other.
CREATE TABLE budget_revision (
    revision_id     text PRIMARY KEY,
    wbs_id          text NOT NULL,
    budget_head_id  text NOT NULL,
    delta_paise     bigint NOT NULL CHECK (delta_paise <> 0),
    effective_from  date NOT NULL,
    justification   text NOT NULL,
    status          text NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT', 'APPROVED', 'REJECTED', 'CANCELLED')),
    budget_line_id  text REFERENCES budget_line (budget_line_id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL,
    decided_at      timestamptz,
    decided_by      text,
    decision_note   text,
    version_no      integer NOT NULL DEFAULT 1,
    CONSTRAINT fk_budget_revision_control_cell
        FOREIGN KEY (wbs_id, budget_head_id)
        REFERENCES budget_control_cell (wbs_id, budget_head_id),
    -- A decided revision (approved or rejected) must carry who decided it and
    -- when; a still-DRAFT one must not appear decided.
    CONSTRAINT ck_budget_revision_decision CHECK (
        (status = 'DRAFT' AND decided_at IS NULL AND decided_by IS NULL) OR
        (status <> 'DRAFT' AND decided_at IS NOT NULL AND decided_by IS NOT NULL)
    ),
    -- Only an APPROVED revision may carry the budget_line it produced.
    CONSTRAINT ck_budget_revision_line_only_if_approved CHECK (
        (status = 'APPROVED') OR (budget_line_id IS NULL)
    )
);

CREATE INDEX ix_budget_revision_cell ON budget_revision (wbs_id, budget_head_id);
CREATE INDEX ix_budget_revision_status ON budget_revision (status);

-- ------------------------------------------------------------ budget_transfer
-- The maker-checker document for moving budget between two cells (which may
-- differ in wbs_id, budget_head_id, or both). `amount_paise` is always
-- positive here -- the magnitude moved; the sign split into a -amount leg on
-- the source and a +amount leg on the destination happens only in the two
-- budget_line rows an approval writes (`from_line_id` / `to_line_id`).
CREATE TABLE budget_transfer (
    transfer_id     text PRIMARY KEY,
    from_wbs_id     text NOT NULL,
    from_head_id    text NOT NULL,
    to_wbs_id       text NOT NULL,
    to_head_id      text NOT NULL,
    amount_paise    bigint NOT NULL CHECK (amount_paise > 0),
    effective_from  date NOT NULL,
    justification   text NOT NULL,
    status          text NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT', 'APPROVED', 'REJECTED', 'CANCELLED')),
    from_line_id    text REFERENCES budget_line (budget_line_id),
    to_line_id      text REFERENCES budget_line (budget_line_id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL,
    decided_at      timestamptz,
    decided_by      text,
    decision_note   text,
    version_no      integer NOT NULL DEFAULT 1,
    CONSTRAINT fk_budget_transfer_from_cell
        FOREIGN KEY (from_wbs_id, from_head_id)
        REFERENCES budget_control_cell (wbs_id, budget_head_id),
    CONSTRAINT fk_budget_transfer_to_cell
        FOREIGN KEY (to_wbs_id, to_head_id)
        REFERENCES budget_control_cell (wbs_id, budget_head_id),
    -- A transfer that names the same cell on both sides moves nothing and
    -- would create a self-referential audit entry claiming otherwise.
    CONSTRAINT ck_budget_transfer_distinct_cells CHECK (
        from_wbs_id <> to_wbs_id OR from_head_id <> to_head_id
    ),
    CONSTRAINT ck_budget_transfer_decision CHECK (
        (status = 'DRAFT' AND decided_at IS NULL AND decided_by IS NULL) OR
        (status <> 'DRAFT' AND decided_at IS NOT NULL AND decided_by IS NOT NULL)
    ),
    CONSTRAINT ck_budget_transfer_lines_only_if_approved CHECK (
        (status = 'APPROVED') OR (from_line_id IS NULL AND to_line_id IS NULL)
    )
);

CREATE INDEX ix_budget_transfer_from ON budget_transfer (from_wbs_id, from_head_id);
CREATE INDEX ix_budget_transfer_to ON budget_transfer (to_wbs_id, to_head_id);
CREATE INDEX ix_budget_transfer_status ON budget_transfer (status);

-- ------------------------------------------------------- budget_version(_cell)
-- Named, frozen snapshots of every cell's own `budget_paise` at the moment
-- the snapshot was taken -- for SCR-10 ("compare this version of the budget
-- to that one"). A snapshot, not a view: domain-controls.md is explicit that
-- a ROLLUP must never be persisted, and this respects that -- each row here
-- is a cell's OWN value, exactly as `budget_control_cell` held it, not a
-- subtree sum. `app/backend/pg/budget.py::compare_versions` does any rollup
-- arithmetic a caller wants at read time, over these frozen own-values.
CREATE TABLE budget_version (
    version_id  text PRIMARY KEY,
    project_id  text NOT NULL REFERENCES project (project_id),
    version_no  integer NOT NULL,
    label       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text NOT NULL,
    UNIQUE (project_id, version_no)
);

CREATE INDEX ix_budget_version_project ON budget_version (project_id);

CREATE TABLE budget_version_cell (
    version_id      text NOT NULL REFERENCES budget_version (version_id),
    wbs_id          text NOT NULL,
    budget_head_id  text NOT NULL,
    budget_paise    bigint NOT NULL CHECK (budget_paise >= 0),
    PRIMARY KEY (version_id, wbs_id, budget_head_id)
);
