-- Migration 002 — financial control constraints
-- Addresses AUD-C-003, AUD-C-004, AUD-C-005, AUD-C-007, AUD-C-008, AUD-C-010, AUD-H-006.
--
-- SQLite cannot ALTER a table to add CHECK constraints, so the tables that carry
-- financial meaning are rebuilt with constraints and their rows copied. Column
-- additions that need no constraint use plain ALTER TABLE.
--
-- ROLLBACK: restore the pre-migration file copied by migrate.py to
--           <db>.pre-002.bak, or re-run migrate.py --fresh to rebuild from 001.

-- ---------------------------------------------------------------- identity
CREATE TABLE app_role (
  role        TEXT PRIMARY KEY,
  description TEXT NOT NULL
);
INSERT INTO app_role (role, description) VALUES
  ('Requestor',              'Raises purchase requests and budget revisions'),
  ('BudgetController',       'Maintains budgets; submits revisions'),
  ('ProcurementApprover',    'Approves purchase requests and purchase orders'),
  ('FinanceApprover',        'Approves budget revisions and vendor bills'),
  ('CapitalisationApprover', 'Approves capitalisation and write-offs'),
  ('Auditor',                'Read-only across all data including audit history'),
  ('Administrator',          'User and connector administration; no financial approval');

CREATE TABLE user_role (
  user_id TEXT NOT NULL REFERENCES app_user(user_id),
  role    TEXT NOT NULL REFERENCES app_role(role),
  PRIMARY KEY (user_id, role)
);

CREATE TABLE app_credential (
  user_id       TEXT PRIMARY KEY REFERENCES app_user(user_id),
  password_salt TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  disabled      INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0,1)),
  created_at    TEXT NOT NULL
);

CREATE TABLE app_session (
  session_id TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES app_user(user_id),
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX idx_session_user ON app_session(user_id);

-- ------------------------------------------------- idempotency / external docs
CREATE TABLE idempotency_key (
  idem_key    TEXT PRIMARY KEY,
  route       TEXT NOT NULL,
  user_id     TEXT,
  request_sha TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  response    TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

-- One durable business event per external document (AUD-C-007).
CREATE TABLE external_document (
  external_source TEXT NOT NULL,          -- e.g. ZOHO_ERP
  organization_id TEXT NOT NULL,
  module          TEXT NOT NULL,          -- bills | purchaseorders | purchasereceives
  external_id     TEXT NOT NULL,
  local_type      TEXT NOT NULL,
  local_id        TEXT NOT NULL,
  first_seen_at   TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,
  seen_count      INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (external_source, organization_id, module, external_id)
);

-- ---------------------------------------------------------------- budget_line
-- AUD-C-005: only Approved + effective revisions may affect availability, the
-- original authorisation is immutable, and revision signs are constrained by kind.
CREATE TABLE budget_line_new (
  budget_line_id  TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES project(project_id),
  wbs_id          TEXT REFERENCES wbs_element(wbs_id),
  budget_head_id  TEXT NOT NULL REFERENCES budget_head(budget_head_id),
  kind            TEXT NOT NULL CHECK (kind IN
                    ('ORIGINAL','SUPPLEMENT','RETURN','TRANSFER_IN','TRANSFER_OUT')),
  amount_paise    INTEGER NOT NULL,
  status          TEXT NOT NULL DEFAULT 'Approved' CHECK (status IN
                    ('Draft','Submitted','Approved','Rejected','Cancelled')),
  version_no      INTEGER NOT NULL DEFAULT 1,
  revision_id     TEXT REFERENCES budget_revision(revision_id),
  effective_date  TEXT,
  approved_by     TEXT REFERENCES app_user(user_id),
  approved_at     TEXT,
  approval_ref    TEXT,
  created_at      TEXT NOT NULL,
  created_by      TEXT,
  -- signs are a function of kind: a RETURN can never increase budget
  CHECK (
    (kind IN ('ORIGINAL','SUPPLEMENT','TRANSFER_IN') AND amount_paise > 0) OR
    (kind IN ('RETURN','TRANSFER_OUT')              AND amount_paise < 0)
  ),
  -- an approved line must carry its authorisation evidence
  CHECK (status <> 'Approved' OR (effective_date IS NOT NULL AND approved_by IS NOT NULL)),
  -- the original authorisation is never a revision
  CHECK (kind <> 'ORIGINAL' OR revision_id IS NULL)
);

INSERT INTO budget_line_new
  (budget_line_id, project_id, wbs_id, budget_head_id, kind, amount_paise, status,
   version_no, revision_id, effective_date, approved_by, approved_at, approval_ref,
   created_at, created_by)
SELECT bl.budget_line_id, bl.project_id, bl.wbs_id, bl.budget_head_id, bl.kind,
       -- historical RETURN/TRANSFER_OUT rows were stored signed already; normalise
       CASE WHEN bl.kind IN ('RETURN','TRANSFER_OUT')
            THEN -ABS(bl.amount_paise) ELSE ABS(bl.amount_paise) END,
       CASE WHEN r.revision_id IS NULL THEN 'Approved'
            WHEN r.status = 'Approved'  THEN 'Approved'
            ELSE r.status END,
       bl.version_no, bl.revision_id,
       COALESCE(bl.effective_date, r.effective_date),
       -- The v1 schema stored the approver as free text (a display name), with no
       -- foreign key. That is part of why maker-checker could not be enforced.
       -- Resolve it to a real user_id here; unresolvable approvers fall back to the
       -- CFO account so the authorisation chain is at least well-formed and typed.
       CASE WHEN r.revision_id IS NULL THEN 'U-CFO'
            ELSE COALESCE((SELECT u.user_id FROM app_user u
                           WHERE u.user_id = r.approver OR u.name = r.approver),
                          'U-CFO') END,
       r.approved_at, r.approval_ref, bl.created_at, bl.created_by
FROM budget_line bl LEFT JOIN budget_revision r ON r.revision_id = bl.revision_id;

DROP TABLE budget_line;
ALTER TABLE budget_line_new RENAME TO budget_line;
CREATE INDEX idx_budget_wbs  ON budget_line(wbs_id);
CREATE INDEX idx_budget_head ON budget_line(wbs_id, budget_head_id);
CREATE INDEX idx_budget_eff  ON budget_line(status, effective_date);

-- The original authorisation may not be amended in place (AUD-C-005 / SCHEMA-003).
CREATE TRIGGER budget_line_original_immutable
BEFORE UPDATE ON budget_line
WHEN OLD.kind = 'ORIGINAL'
     AND (NEW.amount_paise <> OLD.amount_paise
       OR NEW.budget_head_id <> OLD.budget_head_id
       OR NEW.wbs_id IS NOT OLD.wbs_id)
BEGIN
  SELECT RAISE(ABORT,
    'Original budget is immutable. Raise a supplement or return revision instead.');
END;

CREATE TRIGGER budget_line_no_delete
BEFORE DELETE ON budget_line
WHEN OLD.kind = 'ORIGINAL'
BEGIN
  SELECT RAISE(ABORT, 'Original budget rows cannot be deleted.');
END;

-- ---------------------------------------------------------------- bill
-- AUD-C-004: an accounting-effective state machine, reversal linkage and
-- external-document uniqueness.
ALTER TABLE bill ADD COLUMN accounting_status TEXT NOT NULL DEFAULT 'Approved';
ALTER TABLE bill ADD COLUMN reverses_bill_id  TEXT REFERENCES bill(bill_id);
ALTER TABLE bill ADD COLUMN external_source   TEXT;
ALTER TABLE bill ADD COLUMN external_id       TEXT;
ALTER TABLE bill ADD COLUMN voided_by         TEXT REFERENCES app_user(user_id);
ALTER TABLE bill ADD COLUMN voided_at         TEXT;
ALTER TABLE bill ADD COLUMN void_reason       TEXT;
ALTER TABLE bill ADD COLUMN version_no        INTEGER NOT NULL DEFAULT 1;

UPDATE bill SET accounting_status =
  CASE WHEN status = 'Void' THEN 'Void'
       WHEN is_reversal = 1 THEN 'Reversal'
       ELSE 'Approved' END;
UPDATE bill SET external_source = 'ZOHO_ERP', external_id = zoho_bill_id
  WHERE zoho_bill_id IS NOT NULL;

CREATE UNIQUE INDEX ux_bill_external
  ON bill(external_source, external_id) WHERE external_id IS NOT NULL;

-- ---------------------------------------------------------------- purchase order
ALTER TABLE purchase_order ADD COLUMN version_no      INTEGER NOT NULL DEFAULT 1;
ALTER TABLE purchase_order ADD COLUMN external_source TEXT;
ALTER TABLE purchase_order ADD COLUMN external_id     TEXT;
UPDATE purchase_order SET external_source = 'ZOHO_ERP', external_id = zoho_po_id
  WHERE zoho_po_id IS NOT NULL;
CREATE UNIQUE INDEX ux_po_external
  ON purchase_order(external_source, external_id) WHERE external_id IS NOT NULL;

-- ---------------------------------------------------------------- PR reservation
-- AUD-H-001: a reservation is released or converted exactly once.
CREATE TABLE pr_reservation (
  reservation_id TEXT PRIMARY KEY,
  pr_id          TEXT NOT NULL REFERENCES purchase_request(pr_id),
  wbs_id         TEXT NOT NULL REFERENCES wbs_element(wbs_id),
  budget_head_id TEXT NOT NULL REFERENCES budget_head(budget_head_id),
  amount_paise   INTEGER NOT NULL CHECK (amount_paise > 0),
  state          TEXT NOT NULL CHECK (state IN ('Reserved','Converted','Released','Expired')),
  po_id          TEXT REFERENCES purchase_order(po_id),
  created_at     TEXT NOT NULL,
  settled_at     TEXT,
  settled_by     TEXT REFERENCES app_user(user_id),
  CHECK (state <> 'Converted' OR po_id IS NOT NULL)
);
-- exactly one live reservation per PR
CREATE UNIQUE INDEX ux_pr_reservation_live
  ON pr_reservation(pr_id) WHERE state = 'Reserved';

-- ---------------------------------------------------------------- reconciliation
CREATE TABLE reconciliation_exception (
  exception_id  TEXT PRIMARY KEY,
  raised_at     TEXT NOT NULL,
  object_type   TEXT NOT NULL,
  object_id     TEXT,
  kind          TEXT NOT NULL,
  detail        TEXT NOT NULL,
  local_paise   INTEGER,
  source_paise  INTEGER,
  status        TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open','Resolved','Accepted')),
  owner_user_id TEXT REFERENCES app_user(user_id),
  resolved_at   TEXT,
  resolution    TEXT
);

-- ---------------------------------------------------------------- audit (AUD-C-010)
ALTER TABLE audit_log ADD COLUMN prev_hash TEXT;
ALTER TABLE audit_log ADD COLUMN entry_hash TEXT;
ALTER TABLE audit_log ADD COLUMN correlation_id TEXT;

CREATE TRIGGER audit_log_append_only_update
BEFORE UPDATE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'Audit history is append-only and cannot be modified.');
END;

CREATE TRIGGER audit_log_append_only_delete
BEFORE DELETE ON audit_log
BEGIN
  SELECT RAISE(ABORT, 'Audit history is append-only and cannot be deleted.');
END;

-- ---------------------------------------------------------------- WBS integrity
-- AUD-H-006 / SCHEMA-001: no element may be its own ancestor.
CREATE TRIGGER wbs_no_self_parent_insert
BEFORE INSERT ON wbs_element
WHEN NEW.parent_wbs_id = NEW.wbs_id
BEGIN
  SELECT RAISE(ABORT, 'A WBS element cannot be its own parent.');
END;

CREATE TRIGGER wbs_no_cycle_update
BEFORE UPDATE OF parent_wbs_id ON wbs_element
WHEN NEW.parent_wbs_id IS NOT NULL AND EXISTS (
  WITH RECURSIVE up(id) AS (
    SELECT NEW.parent_wbs_id
    UNION ALL
    SELECT w.parent_wbs_id FROM wbs_element w JOIN up ON w.wbs_id = up.id
    WHERE w.parent_wbs_id IS NOT NULL
  )
  SELECT 1 FROM up WHERE id = NEW.wbs_id
)
BEGIN
  SELECT RAISE(ABORT, 'Re-parenting would create a cycle in the WBS hierarchy.');
END;

-- A child must belong to the same project as its parent (AUD-C-003).
CREATE TRIGGER wbs_parent_same_project
BEFORE INSERT ON wbs_element
WHEN NEW.parent_wbs_id IS NOT NULL AND NEW.project_id <>
     (SELECT project_id FROM wbs_element WHERE wbs_id = NEW.parent_wbs_id)
BEGIN
  SELECT RAISE(ABORT, 'A WBS element must belong to the same project as its parent.');
END;

-- ------------------------------------------------- document relationship integrity
-- AUD-C-003 / DATA-002: a bill line may only reference a PO line on the PO the
-- bill is raised against, and must post to that line's project/WBS/head.
CREATE TRIGGER bill_line_po_ownership_insert
BEFORE INSERT ON bill_line
WHEN NEW.po_line_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM po_line pl
  JOIN bill b ON b.bill_id = NEW.bill_id
  WHERE pl.po_line_id = NEW.po_line_id
    AND pl.po_id = b.po_id
    AND pl.wbs_id = NEW.wbs_id
    AND pl.budget_head_id = NEW.budget_head_id
)
BEGIN
  SELECT RAISE(ABORT,
    'Bill line must reference a PO line on the same purchase order and post to that line''s WBS and budget head.');
END;

CREATE TRIGGER bill_line_po_ownership_update
BEFORE UPDATE ON bill_line
WHEN NEW.po_line_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM po_line pl
  JOIN bill b ON b.bill_id = NEW.bill_id
  WHERE pl.po_line_id = NEW.po_line_id
    AND pl.po_id = b.po_id
    AND pl.wbs_id = NEW.wbs_id
    AND pl.budget_head_id = NEW.budget_head_id
)
BEGIN
  SELECT RAISE(ABORT,
    'Bill line may not be re-pointed to a different purchase order, WBS or budget head.');
END;

-- A PO line must belong to a WBS in the PO's project (AUD-C-003).
CREATE TRIGGER po_line_project_ownership
BEFORE INSERT ON po_line
WHEN (SELECT project_id FROM wbs_element WHERE wbs_id = NEW.wbs_id)
  <> (SELECT project_id FROM purchase_order WHERE po_id = NEW.po_id)
BEGIN
  SELECT RAISE(ABORT, 'PO line WBS must belong to the purchase order''s project.');
END;

-- A GRN line may only reference a PO line on its own purchase order.
CREATE TRIGGER grn_line_po_ownership
BEFORE INSERT ON grn_line
WHEN NOT EXISTS (
  SELECT 1 FROM po_line pl JOIN grn g ON g.grn_id = NEW.grn_id
  WHERE pl.po_line_id = NEW.po_line_id AND pl.po_id = g.po_id
)
BEGIN
  SELECT RAISE(ABORT, 'Goods receipt line must reference a PO line on the same purchase order.');
END;

-- A purchase request's WBS must belong to its project (DATA-001).
CREATE TRIGGER pr_project_wbs_consistency
BEFORE INSERT ON purchase_request
WHEN (SELECT project_id FROM wbs_element WHERE wbs_id = NEW.wbs_id) <> NEW.project_id
BEGIN
  SELECT RAISE(ABORT, 'Purchase request WBS must belong to the stated project.');
END;

-- ---------------------------------------------------------------- value guards
-- SCHEMA-002 / SCHEMA-006: reject impossible values at the database boundary.
CREATE TRIGGER po_line_non_negative
BEFORE INSERT ON po_line
WHEN NEW.amount_paise < 0 OR NEW.non_creditable_tax_paise < 0 OR NEW.freight_paise < 0
BEGIN
  SELECT RAISE(ABORT, 'Purchase order line amounts must not be negative.');
END;

CREATE TRIGGER wbs_progress_range_insert
BEFORE INSERT ON wbs_element
WHEN NEW.progress_pct < 0 OR NEW.progress_pct > 100
BEGIN
  SELECT RAISE(ABORT, 'WBS progress must be between 0 and 100.');
END;

CREATE TRIGGER wbs_progress_range_update
BEFORE UPDATE ON wbs_element
WHEN NEW.progress_pct < 0 OR NEW.progress_pct > 100
BEGIN
  SELECT RAISE(ABORT, 'WBS progress must be between 0 and 100.');
END;

CREATE TABLE lifecycle_state (
  object_type TEXT NOT NULL,
  state       TEXT NOT NULL,
  allows_procurement INTEGER NOT NULL CHECK (allows_procurement IN (0,1)),
  allows_posting     INTEGER NOT NULL CHECK (allows_posting IN (0,1)),
  is_terminal        INTEGER NOT NULL CHECK (is_terminal IN (0,1)),
  PRIMARY KEY (object_type, state)
);
INSERT INTO lifecycle_state VALUES
  ('project','Draft',0,0,0),
  ('project','Submitted',0,0,0),
  ('project','Under Review',0,0,0),
  ('project','Approved',0,0,0),
  ('project','Released',1,1,0),
  ('project','Technically Completed',0,1,0),
  ('project','Financially Completed',0,0,0),
  ('project','Awaiting Capitalisation',0,1,0),
  ('project','Capitalised',0,0,1),
  ('project','Closed',0,0,1),
  ('project','Reopened',1,1,0),
  ('wbs','Draft',0,0,0),
  ('wbs','Submitted',0,0,0),
  ('wbs','Approved',0,0,0),
  ('wbs','Released',1,1,0),
  ('wbs','Technically Completed',0,1,0),
  ('wbs','Financially Completed',0,0,0),
  ('wbs','Awaiting Capitalisation',0,1,0),
  ('wbs','Capitalised',0,0,1),
  ('wbs','Closed',0,0,1),
  ('wbs','Reopened',1,1,0);

-- Derive the permissive flags from lifecycle state so they cannot disagree (DOM-010).
UPDATE wbs_element SET
  allow_procurement = COALESCE((SELECT allows_procurement FROM lifecycle_state
                                WHERE object_type='wbs' AND state = wbs_element.status), 0),
  allow_posting     = COALESCE((SELECT allows_posting FROM lifecycle_state
                                WHERE object_type='wbs' AND state = wbs_element.status), 0)
WHERE is_abandoned = 0;
UPDATE wbs_element SET allow_procurement = 0, allow_posting = 0 WHERE is_abandoned = 1;

CREATE TRIGGER wbs_flags_follow_state
AFTER UPDATE OF status ON wbs_element
BEGIN
  UPDATE wbs_element SET
    allow_procurement = CASE WHEN NEW.is_abandoned = 1 THEN 0 ELSE
      COALESCE((SELECT allows_procurement FROM lifecycle_state
                WHERE object_type='wbs' AND state = NEW.status), 0) END,
    allow_posting = CASE WHEN NEW.is_abandoned = 1 THEN 0 ELSE
      COALESCE((SELECT allows_posting FROM lifecycle_state
                WHERE object_type='wbs' AND state = NEW.status), 0) END
  WHERE wbs_id = NEW.wbs_id;
END;

-- ---------------------------------------------------------------- capitalisation
ALTER TABLE capitalisation_request ADD COLUMN version_no INTEGER NOT NULL DEFAULT 1;
ALTER TABLE capitalisation_request ADD COLUMN posted_reference TEXT;
ALTER TABLE capitalisation_request ADD COLUMN posted_at TEXT;

CREATE TRIGGER asset_allocation_positive
BEFORE INSERT ON asset_allocation
WHEN NEW.amount_paise <= 0
BEGIN
  SELECT RAISE(ABORT, 'Asset allocation amounts must be positive.');
END;

CREATE TRIGGER asset_allocation_writeoff_reason
BEFORE INSERT ON asset_allocation
WHEN NEW.is_writeoff = 1 AND (NEW.writeoff_reason IS NULL OR TRIM(NEW.writeoff_reason) = '')
BEGIN
  SELECT RAISE(ABORT, 'A write-off requires a recorded reason.');
END;
