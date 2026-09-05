-- 009_document_approval_states.sql
-- Wave 4 integration: let a document say it is under approval, or returned.
--
-- THE DEFECT THIS CLOSES
--
-- 003 gave `budget_revision.status` and `budget_transfer.status` the domain
-- `('DRAFT', 'APPROVED', 'REJECTED', 'CANCELLED')`. That was adequate while
-- approval was a single service call. It is not adequate now that an approval
-- INSTANCE stands between submission and decision, because the document has no
-- way to say either of the two things that instance makes true:
--
--   * SUBMITTED -- routed, an instance is open, and the maker can no longer
--     edit it. Without the value, a submitted revision stayed DRAFT, so the
--     document's own status ASSERTED SOMETHING FALSE about it. Nothing but
--     `_assert_not_under_approval` in `pg/budget.py` stood between that row and
--     a second, parallel approval down the direct route -- one guard, in one
--     language, holding a property the schema should be stating.
--
--   * RETURNED -- an approver sent it back for correction.
--     `C15_approval_statuses.json` maps the instance status RETURNED to the
--     business status RETURNED, and `C3_statuses.json` carries RETURNED among
--     its frozen 21. The column could not store it, so the write-back wrote
--     DRAFT and recorded the real outcome in `decision_note` prose. A
--     reconciliation reading `status` could not distinguish "never submitted"
--     from "submitted and sent back", and prose is not a status.
--
-- Both values already exist in the frozen registry. This migration is not
-- introducing vocabulary; it is letting the table speak the vocabulary the
-- contracts already froze.
--
-- WHY THE DECISION CONSTRAINT MOVES TOO
--
-- `ck_budget_revision_decision` reads "DRAFT means no decision; anything else
-- means a decision". Adding SUBMITTED to the domain without touching it would
-- have required every submitted revision to carry `decided_at`/`decided_by` --
-- fabricating a decision at the exact moment the point is that none has been
-- taken. The undecided set becomes {DRAFT, SUBMITTED}. RETURNED stays on the
-- decided side, because returning IS a decision, taken by a named approver at
-- a known time, and the constraint should keep saying so.
--
-- EXPAND ONLY (plan v1.2.1 §17.1)
--
-- Every change here WIDENS. No existing row can violate the new form, no
-- reader breaks, and the old application runs unmodified against the new
-- schema -- so this is deployable and reversible on its own, with no
-- compatibility window to wait out.
--
-- NOT DONE HERE, DELIBERATELY: 'CANCELLED' remains in both domains even though
-- it is NOT one of C3's frozen 21 and nothing in the application writes it
-- (`approval_writeback` maps an administrative cancel to DRAFT rather than
-- leak a status no business screen has a label for). Removing a permitted
-- value is a CONTRACT step, and §17.1 forbids bundling one with the expand
-- that replaces it. It is recorded as an open item rather than smuggled in.

BEGIN;

-- ---------------------------------------------------------------- revision --
ALTER TABLE budget_revision
    DROP CONSTRAINT IF EXISTS budget_revision_status_check;
ALTER TABLE budget_revision
    ADD CONSTRAINT budget_revision_status_check CHECK (
        status IN ('DRAFT', 'SUBMITTED', 'RETURNED',
                   'APPROVED', 'REJECTED', 'CANCELLED')
    );

ALTER TABLE budget_revision
    DROP CONSTRAINT IF EXISTS ck_budget_revision_decision;
ALTER TABLE budget_revision
    ADD CONSTRAINT ck_budget_revision_decision CHECK (
        (status IN ('DRAFT', 'SUBMITTED')
             AND decided_at IS NULL AND decided_by IS NULL) OR
        (status NOT IN ('DRAFT', 'SUBMITTED')
             AND decided_at IS NOT NULL AND decided_by IS NOT NULL)
    );

-- ---------------------------------------------------------------- transfer --
ALTER TABLE budget_transfer
    DROP CONSTRAINT IF EXISTS budget_transfer_status_check;
ALTER TABLE budget_transfer
    ADD CONSTRAINT budget_transfer_status_check CHECK (
        status IN ('DRAFT', 'SUBMITTED', 'RETURNED',
                   'APPROVED', 'REJECTED', 'CANCELLED')
    );

ALTER TABLE budget_transfer
    DROP CONSTRAINT IF EXISTS ck_budget_transfer_decision;
ALTER TABLE budget_transfer
    ADD CONSTRAINT ck_budget_transfer_decision CHECK (
        (status IN ('DRAFT', 'SUBMITTED')
             AND decided_at IS NULL AND decided_by IS NULL) OR
        (status NOT IN ('DRAFT', 'SUBMITTED')
             AND decided_at IS NOT NULL AND decided_by IS NOT NULL)
    );

-- `ck_*_line_only_if_approved` is deliberately untouched: only an APPROVED
-- document may carry the budget_line it produced, and neither new value is
-- APPROVED. A SUBMITTED or RETURNED row carrying a line would still be
-- refused, which is correct -- routing a document creates no budget.

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- Narrowing again REQUIRES that no row holds either new value. Check
--   -- before running, because the ALTER will fail loudly rather than discard
--   -- data -- which is the correct behaviour and the reason this is not
--   -- automated:
--   --
--   --   SELECT status, count(*) FROM budget_revision
--   --    WHERE status IN ('SUBMITTED','RETURNED') GROUP BY status;
--   --   SELECT status, count(*) FROM budget_transfer
--   --    WHERE status IN ('SUBMITTED','RETURNED') GROUP BY status;
--   --
--   -- A non-empty result means documents are mid-approval. Let them settle;
--   -- do not rewrite them to DRAFT, which would silently release them for a
--   -- second, parallel approval.
--
--   ALTER TABLE budget_revision DROP CONSTRAINT budget_revision_status_check;
--   ALTER TABLE budget_revision ADD CONSTRAINT budget_revision_status_check
--       CHECK (status IN ('DRAFT', 'APPROVED', 'REJECTED', 'CANCELLED'));
--   ALTER TABLE budget_revision DROP CONSTRAINT ck_budget_revision_decision;
--   ALTER TABLE budget_revision ADD CONSTRAINT ck_budget_revision_decision
--       CHECK ((status = 'DRAFT' AND decided_at IS NULL AND decided_by IS NULL)
--           OR (status <> 'DRAFT' AND decided_at IS NOT NULL
--               AND decided_by IS NOT NULL));
--
--   ALTER TABLE budget_transfer DROP CONSTRAINT budget_transfer_status_check;
--   ALTER TABLE budget_transfer ADD CONSTRAINT budget_transfer_status_check
--       CHECK (status IN ('DRAFT', 'APPROVED', 'REJECTED', 'CANCELLED'));
--   ALTER TABLE budget_transfer DROP CONSTRAINT ck_budget_transfer_decision;
--   ALTER TABLE budget_transfer ADD CONSTRAINT ck_budget_transfer_decision
--       CHECK ((status = 'DRAFT' AND decided_at IS NULL AND decided_by IS NULL)
--           OR (status <> 'DRAFT' AND decided_at IS NOT NULL
--               AND decided_by IS NOT NULL));
--   COMMIT;
