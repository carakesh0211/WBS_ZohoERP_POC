-- 013_procurement.sql
-- The procurement document chain, which migrations 001..012 never created at all.
--
-- WHAT THIS CLOSES
--
-- `docs/FULL_APPLICATION_DELIVERY_STATUS.md` claimed "Phase 1 -- PostgreSQL
-- port, behaviour-identical | complete". It was not. Migrations 001..012 create
-- 53 tables and not ONE of them is a procurement document. The consequence was
-- not a missing feature, it was an exit criterion nobody could reach: Wave 5's
-- "ordered, received, billed and open reconcile to the paisa" needs four
-- quantities and three of them had nowhere to live.
--
-- `/api/integrations/reconciliation` already refuses for this reason.
-- `app/backend/integration/sweeps.py` attributes every receive line to a
-- `po_line` that has no PostgreSQL home, and quarantines the rest. This
-- migration is that home.
--
-- Everything below is traced to `app/backend/db.py::SCHEMA` (the SQLite POC),
-- `app/backend/migrations/002_financial_controls.sql` (the ALTERs and the six
-- triggers it is replacing), plan §6.1/§6.2, or `docs/WAVE6_PROCUREMENT_CONTRACT.md`.
-- Where something was absent from all of them it is named as a GAP below rather
-- than improvised into existence.
--
--
-- THE SIX TRIGGERS THIS REPLACES, AND WHAT EACH GAINS
--
-- Plan §6.2: "Declarative forms are strictly stronger: a composite FK also
-- covers UPDATE and ON DELETE, which several triggers did not." Every one of
-- the six SQLite triggers below was BEFORE INSERT only, except the bill_line
-- pair. So the UPDATE case is not a bonus here -- it is a hole each of these
-- closes.
--
--   bill_line_po_ownership_insert / _update
--       -> fk_bill_line_po_line_cell, four columns, ON UPDATE RESTRICT.
--          The trigger pair checked the bill line. It did NOT stop the PO LINE
--          being re-pointed underneath the bill afterwards -- change
--          `po_line.wbs_id` and every bill line already posted against it is
--          silently posting to a cell it was never checked against. ON UPDATE
--          RESTRICT is what makes that impossible rather than merely unlikely.
--
--   po_line_project_ownership
--       -> fk_po_line_po_project + fk_po_line_wbs_project, via a denormalised
--          `po_line.project_id`. One FK ties the line's project to its purchase
--          order's, the other ties its WBS to that same project. Together they
--          say exactly what the trigger said, on UPDATE as well as INSERT.
--
--   grn_line_po_ownership
--       -> fk_grn_line_po_line + fk_grn_line_grn_po, via a denormalised
--          `grn_line.po_id`. BOTH are needed and the contract names only the
--          first. `(po_line_id, po_id) -> po_line` ties the receive line to a
--          PO line on the PO the LINE claims; nothing in it ties that claim to
--          the GRN's own `po_id`, which is the half the trigger actually
--          enforced (`pl.po_id = g.po_id`). fk_grn_line_grn_po is that half.
--
--   pr_project_wbs_consistency
--       -> fk_pr_line_wbs_project + fk_pr_line_pr_project. Same shape, one
--          level down, because the WBS moved to the line (GAP-1).
--
--   po_line_non_negative
--       -> ck_po_line_non_negative, covering UPDATE, which the INSERT-only
--          trigger did not.
--
-- The same "the composite FK does not tie the denormalised column to the
-- parent" argument applies to `bill_line.po_id`, and is closed by
-- fk_bill_line_bill_po plus ck_bill_line_po_id_accompanies_po_line -- see
-- the bill_line section.
--
--
-- CONTRACT GAP 1: pr_line EXISTS IN NO FROZEN CONTRACT
--
-- `docs/WAVE6_PROCUREMENT_CONTRACT.md` §GAP-1, restated because it changes a
-- financial-control surface. The SQLite POC's `purchase_request` is
-- HEADER-ONLY: one `amount_paise`, one `wbs_id`, one `budget_head_id`, so a PR
-- addresses exactly one control cell, and `domain.budget_check`,
-- `pr_reservation` and `convert_pr_to_po` all assume that. `C4_entities.json`
-- freezes 44 entities and there is no `PR Line` among them.
--
-- The product owner asked for `pr_line`, so it is built, and the header keeps
-- `amount_paise` as a DERIVED total maintained by
-- `capex_purchase_request_total_refresh()` so the existing header-level reads
-- keep their meaning while the lines carry the control-cell grain. No entity is
-- added to `C4_entities.json`: its QA gate counts 44 and editing a frozen
-- registry to fit an implementation is the improvisation this project bans.
--
--
-- CONTRACT GAP 2: grn HAD NO EXTERNAL-DOCUMENT BLOCK
--
-- `bill` and `purchase_order` were given `external_source` / `external_id` by
-- `app/backend/migrations/002_financial_controls.sql`. `grn` was not -- it
-- carried a bare `zoho_receive_id`. Plan §6.1 requires the four-column block
-- "on every Zoho-mirrored table", and a goods receipt is mirrored from Zoho by
-- definition. The block is added, with `ux_grn_external` partial on
-- `external_id IS NOT NULL`. This is the plan's own rule applied, and it is the
-- one place Wave 6 gives a table something the POC did not have.
--
--
-- CONTRACT GAP 3: TWO STATUS COLUMNS HAD NO CHECK
--
-- `purchase_request.status` and `bill.accounting_status` were free text in
-- SQLite with their permitted values written only in a trailing comment. Both
-- get named CHECKs. This makes previously acceptable rows invalid, so what is
-- rejected is stated rather than discovered:
--
--   * `purchase_request.status` is constrained to the seven C3 labels the POC
--     actually writes plus `Returned`. Draft / Submitted / Under Review /
--     Exception Pending / Approved / Rejected are the six `services.py`
--     produces (`create_pr`, `approve_pr`) and `db.py::seed` seeds; `Returned`
--     is C3's own label for "sent back to requestor" and is admitted on
--     009_document_approval_states.sql's precedent, which added exactly that
--     value to two other document tables for exactly that reason.
--
--     The LABEL form ('Draft'), not the CODE form ('DRAFT'), deliberately.
--     003/009 constrain `budget_revision.status` in codes, so this is
--     inconsistent with them, and the inconsistency is chosen: the POC's
--     procurement code compares against labels, the frozen contract spells
--     `bill.accounting_status`'s value set in labels three lines further down,
--     and a port that silently re-spells the values it ports is not a port.
--     Both spellings are C3's; C3 carries a `code` and a `label` for each of
--     its 21 statuses and neither is invented here.
--
--   * `bill.accounting_status` is constrained to AUD-C-004's four:
--     Draft / Approved / Void / Reversal. Only Approved and Reversal are
--     accounting-effective. Reversal negates BY FLAG, never by data entry.
--
-- `purchase_order.status`, `grn.status` and `bill.status` are left
-- unconstrained. The frozen contract names two columns and these are not among
-- them; constraining a third would reject rows whose permitted set nothing in
-- this repository states, which is how a schema silently becomes the author's
-- opinion.
--
--
-- CONTRACT GAP 4 (NEW, RAISED BY THIS MIGRATION): bill COULD NOT BE SCOPED
--
-- The frozen contract requires every new table to carry a policy with USING and
-- WITH CHECK. `bill` cannot have one. Its only dimension-bearing column in the
-- POC is `po_id`, and `po_id` is NULLABLE because non-PO bills exist. That
-- leaves three options and two of them are the defects 012 was written to
-- close:
--
--   * scope through `po_id` and waive on NULL -> every non-PO bill readable by
--     every principal. A leak, and precisely 011's unattributed-exception bug.
--   * scope through `po_id` with no waiver -> every non-PO bill invisible to
--     everyone. A silent drop, and precisely the other half of that same bug.
--   * give `bill` a `project_id`.
--
-- `bill.project_id text NOT NULL REFERENCES project` is therefore added. It is
-- denormalisation for scopability, exactly as `approval_instance.entity_id` /
-- `.project_id` are denormalised at creation "precisely so this table is
-- scopable without a join" (WAVE4 contract 6). It is FK-tied to the purchase
-- order's project by `fk_bill_po_project` whenever `po_id` is present, so it
-- cannot drift from the PO it bills. This is a column the frozen contract does
-- not list, and it is named here rather than slipped in.
--
--
-- WHY EVERY POLICY JOINS `project` AND FILTERS ALL FOUR DIMENSIONS
--
-- The obvious predicate for a table carrying `project_id` is
-- `capex_scope_permits(NULL, NULL, NULL, project_id)`, which is what
-- `budget_version_scope` does in 006, and it would be wrong here.
--
-- Scope dimensions are resolved INDEPENDENTLY (`pg/principal_scope.py`,
-- property 2): a principal restricted to one ENTITY and to no project at all
-- carries `entity_ids={ENT-A}` and `project_ids=None`. `capex_dimension_permits`
-- treats `None` as unrestricted, so a project-only predicate returns TRUE for
-- every row and that principal reads every entity's purchase orders. For
-- `wbs_element` that waiver is documented and accepted -- the primary control
-- `repo.compile_scope` joins through `project`, and RLS is the backstop. For a
-- procurement document it is not acceptable: these rows ARE the commitments and
-- the money, and "entity A cannot read entity B's orders" has to hold at the
-- backstop too, not only in the compiler.
--
-- Every policy below therefore reaches `project` and passes all four of its
-- dimension columns:
--
--     capex_scope_permits(p.entity_id, p.plant_id, p.location_id, p.project_id)
--
-- ARGUMENT ORDER MATTERS AND IS EASY TO GET WRONG. The signature is
-- `capex_scope_permits(p_entity_id, p_plant_id, p_location_id, p_project_id)`
-- (004). All four parameters are `text`, so PostgreSQL accepts ANY order
-- silently. Migration 011 put `project_id` in the LOCATION slot and the project
-- dimension went entirely unenforced for a whole wave before 012 found it.
-- Every call below names its arguments in the same left-to-right order as the
-- `project` columns they come from, so a transposition is visible on one line.
--
-- `EXISTS`, so a row whose `project_id` matches no `project` is DENIED, not
-- admitted. Fail closed.
--
--
-- MONEY
--
-- Every `*_paise` column is `bigint`, integer paise, never `numeric` and never
-- a float, and every one of them STARTS its declaration line.
-- `migrate_pg.py::_PAISE_COLUMN_RE` is anchored at `^` against each stripped
-- field of a `CREATE TABLE` body: a paise column the parser cannot see is a
-- money column whose type adoption never verifies, and drift to
-- `numeric(18,2)` then certifies as adopted while being exactly the
-- Decimal-leakage path the domain forbids.
--
-- `quantity` is `numeric`, not `real`. The POC used SQLite `REAL`; a receipt
-- quantity of 0.2 is not representable in binary floating point and three
-- partial receipts that should total a full line would not.
--
-- `purchase_order.exchange_rate` is `numeric` for the same reason, ported from
-- the POC's `REAL NOT NULL DEFAULT 1.0`.
--
--
-- WHY grn_line's quantity AND amount_paise ARE SIGNED
--
-- They carry no non-negativity CHECK, deliberately, and this is the one place
-- where adding the "obvious" constraint would break the product. The POC seeds
-- a reversal receipt line at quantity -0.2 and amount -1,20,000 paise. AUD-C-004
-- is reversal-BY-FLAG: a reversal is a NEW row carrying `grn.is_reversal =
-- true`, never an edit of the original and never a delete. A `>= 0` CHECK here
-- rejects exactly the rows the reversal contract requires.
--
-- `bill_line.amount_paise` is signed for the same reason: a credit note's line
-- amounts are negative paise (§2.4 of the frozen contract), and `render_paise`
-- has to sign them correctly -- `divmod` floors, so -150 renders as -2.50
-- unless the magnitude is divided and the sign reapplied.
--
--
-- EVERY CONSTRAINT IS NAMED
--
-- `migrate_pg.py::_named_constraints_by` reports only constraints declared with
-- an explicit `CONSTRAINT name`, because an anonymous `UNIQUE (col)` or inline
-- `CHECK (...)` has no `pg_constraint.conname` to look up. An unnamed
-- constraint is therefore invisible to adoption verification: a database
-- missing it certifies as "adopted" while the constraint is absent. Not one
-- table-level constraint below is anonymous.
--
-- The one exception is deliberate and is NOT this migration's: `wbs_element`
-- already carries `UNIQUE (wbs_id, project_id)` from 002_budget_control.sql,
-- unnamed. It is a valid target for the two composite FKs below and it is left
-- exactly as it is. Adding a second, named UNIQUE over the same two columns
-- would build a duplicate index for a name, and renaming 002's auto-generated
-- constraint would make THIS migration fail to adopt on any database where it
-- has already run (a rename raises `undefined_object`, which is not in
-- `_DUPLICATE_OBJECT_ERRORS` and so is a hard error rather than an adoption).
--
--
-- IMMUTABILITY IS A PRIVILEGE, NOT ONLY A TRIGGER
--
-- Posted or imported financial records are immutable except through an explicit
-- correction/reversal workflow. Following the `audit_log` precedent in 001/004
-- and `approval_action`'s in 008, `capex_app` gets SELECT/INSERT/UPDATE and NO
-- DELETE on any of the eight tables.
--
-- Omitting the DELETE grant is NOT enough, and this is the trap. 004 issued
-- `ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE,
-- DELETE ON TABLES TO capex_app`, which attaches to the role that runs the
-- migrations and therefore applies to every table created by every LATER
-- migration -- including these eight, from the instant each of them is
-- created.
-- Saying nothing about DELETE would leave capex_app holding it. The REVOKE at
-- the foot of this file is what actually removes it, exactly as 004 revokes it
-- back off `audit_log` and 008 off `approval_action` in the same migration that
-- granted it.

BEGIN;

-- ============================================================ purchase_request
-- Header-only in the POC. `wbs_id`, `budget_head_id` and `description` move to
-- `pr_line` (GAP-1); `amount_paise` stays as a DERIVED total.
CREATE TABLE purchase_request (
    pr_id            text PRIMARY KEY,
    pr_number        text NOT NULL,
    project_id       text NOT NULL REFERENCES project (project_id),

    requested_by     text NOT NULL,
    requested_at     timestamptz NOT NULL DEFAULT now(),
    status           text NOT NULL DEFAULT 'Draft',
    -- WITHIN_BUDGET | EXCEEDS_BUDGET, written by `domain.budget_check`.
    check_result     text,
    approver         text,
    approved_at      timestamptz,
    exception_reason text,
    reserves_budget  boolean NOT NULL DEFAULT false,

    -- DERIVED. Maintained equal to SUM(pr_line.amount_paise) by
    -- `capex_purchase_request_total_refresh()`. See GAP-1, and the trigger's
    -- own note on why this is a trigger and cannot be a CHECK.
amount_paise     bigint NOT NULL DEFAULT 0,

    -- §6.1's external-document block.
    external_source        text,
    external_id            text,
    external_last_modified timestamptz,
    payload_sha            text,

    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by       text NOT NULL,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    updated_by       text NOT NULL,
    version_no       integer NOT NULL DEFAULT 1,

    CONSTRAINT ux_purchase_request_number UNIQUE (pr_number),

    -- Exists only to be an FK target: `fk_pr_line_pr_project` ties a line's
    -- denormalised project to the header's, so the two can never diverge.
    CONSTRAINT ux_purchase_request_id_project UNIQUE (pr_id, project_id),

    -- GAP-3. C3 labels; see the header for why the label form and not the code.
    CONSTRAINT ck_purchase_request_status CHECK (
        status IN ('Draft', 'Submitted', 'Under Review', 'Exception Pending',
                   'Approved', 'Rejected', 'Returned')
    ),
    CONSTRAINT ck_purchase_request_check_result CHECK (
        check_result IS NULL
        OR check_result IN ('WITHIN_BUDGET', 'EXCEEDS_BUDGET')
    ),
    -- A request is for a positive sum or for nothing; the derived total is
    -- never negative. Signed money belongs on `grn_line` and `bill_line`, where
    -- reversals and credit notes live, and nowhere else in this chain.
    CONSTRAINT ck_purchase_request_amount_nonneg CHECK (amount_paise >= 0)
);

CREATE INDEX ix_purchase_request_project ON purchase_request (project_id);

-- Ports the POC's external-document uniqueness shape 1:1. PARTIAL, so the many
-- locally-raised requests that were never mirrored from Zoho do not all collide
-- on a single NULL.
CREATE UNIQUE INDEX ux_purchase_request_external
    ON purchase_request (external_source, external_id)
    WHERE external_id IS NOT NULL;

-- ==================================================================== pr_line
-- New (GAP-1). `project_id` is denormalised so the composite FKs have something
-- to bind: PostgreSQL cannot express "this line's WBS belongs to this line's
-- parent's project" without carrying the project on the line.
CREATE TABLE pr_line (
    pr_line_id     text PRIMARY KEY,
    pr_id          text NOT NULL REFERENCES purchase_request (pr_id),
    line_no        integer NOT NULL,

    -- Denormalised for the two composite FKs below. Never set independently:
    -- fk_pr_line_pr_project forces it to equal the header's.
    project_id     text NOT NULL,
    wbs_id         text NOT NULL,
    budget_head_id text NOT NULL REFERENCES budget_head (budget_head_id),

    description    text,
    quantity       numeric NOT NULL DEFAULT 1,
amount_paise   bigint NOT NULL,

    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,

    -- The POC had no such constraint on `po_line`, so duplicate line numbers
    -- within one document were possible and a "line 3" could mean two rows.
    CONSTRAINT ux_pr_line_number UNIQUE (pr_id, line_no),

    -- Replaces trigger `pr_project_wbs_consistency`, and covers UPDATE, which
    -- the BEFORE INSERT trigger did not.
    CONSTRAINT fk_pr_line_wbs_project
        FOREIGN KEY (wbs_id, project_id)
        REFERENCES wbs_element (wbs_id, project_id) ON UPDATE RESTRICT,

    -- ...and the half the trigger enforced implicitly by reading NEW.project_id
    -- off the header row: the line's project IS the header's project.
    CONSTRAINT fk_pr_line_pr_project
        FOREIGN KEY (pr_id, project_id)
        REFERENCES purchase_request (pr_id, project_id) ON UPDATE RESTRICT,

    CONSTRAINT ck_pr_line_amount_nonneg CHECK (amount_paise >= 0)
);

CREATE INDEX ix_pr_line_pr   ON pr_line (pr_id);
CREATE INDEX ix_pr_line_cell ON pr_line (wbs_id, budget_head_id);

-- ============================================================== purchase_order
CREATE TABLE purchase_order (
    po_id           text PRIMARY KEY,
    po_number       text NOT NULL,
    pr_id           text REFERENCES purchase_request (pr_id),
    project_id      text NOT NULL REFERENCES project (project_id),

    vendor_name     text NOT NULL,
    currency        text NOT NULL DEFAULT 'INR',
    -- numeric, never real: a rate is multiplied into money.
    exchange_rate   numeric NOT NULL DEFAULT 1,
    status          text NOT NULL DEFAULT 'Draft',
    ordered_at      timestamptz,
    amendment_no    integer NOT NULL DEFAULT 0,
    closed_residual_released boolean NOT NULL DEFAULT false,

    external_source        text,
    external_id            text,
    external_last_modified timestamptz,
    payload_sha            text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL,
    version_no      integer NOT NULL DEFAULT 1,

    CONSTRAINT ux_purchase_order_number UNIQUE (po_number),

    -- Exists only to be an FK target -- `fk_po_line_po_project` and
    -- `fk_bill_po_project` both bind against it.
    CONSTRAINT ux_po_id_project UNIQUE (po_id, project_id),

    CONSTRAINT ck_purchase_order_amendment_nonneg CHECK (amendment_no >= 0),
    CONSTRAINT ck_purchase_order_exchange_rate_positive CHECK (exchange_rate > 0)
);

CREATE INDEX ix_purchase_order_project ON purchase_order (project_id);
CREATE INDEX ix_purchase_order_pr ON purchase_order (pr_id) WHERE pr_id IS NOT NULL;

-- `ux_po_external` from app/backend/migrations/002_financial_controls.sql,
-- ported 1:1 -- PostgreSQL supports partial unique indexes natively (§6.2).
CREATE UNIQUE INDEX ux_po_external
    ON purchase_order (external_source, external_id)
    WHERE external_id IS NOT NULL;

-- ==================================================================== po_line
-- Keyed on `(wbs_id, budget_head_id)` -- the grain `outbound.py` already states
-- and emits against. A multi-WBS purchase order is normal and is NOT flattened
-- into its header.
CREATE TABLE po_line (
    po_line_id     text PRIMARY KEY,
    po_id          text NOT NULL REFERENCES purchase_order (po_id),
    line_no        integer NOT NULL,

    -- Denormalised (§6.2). Bound to the PO's project by fk_po_line_po_project.
    project_id     text NOT NULL,
    wbs_id         text NOT NULL,
    budget_head_id text NOT NULL REFERENCES budget_head (budget_head_id),

    description    text,
    quantity       numeric NOT NULL DEFAULT 1,
rate_paise     bigint NOT NULL,
amount_paise   bigint NOT NULL,
tax_paise      bigint NOT NULL DEFAULT 0,
non_creditable_tax_paise bigint NOT NULL DEFAULT 0,
freight_paise  bigint NOT NULL DEFAULT 0,

    -- Ports `po_line.zoho_line_item_id`. `sweeps.py::resolve_po_line` looks a
    -- receive line up by (PO external id, line external id); this is that
    -- second half.
    line_external_id text,

    created_at     timestamptz NOT NULL DEFAULT now(),
    created_by     text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    updated_by     text NOT NULL,
    version_no     integer NOT NULL DEFAULT 1,

    CONSTRAINT ux_po_line_number UNIQUE (po_id, line_no),

    -- The target of `fk_grn_line_po_line`.
    CONSTRAINT ux_po_line_po UNIQUE (po_line_id, po_id),

    -- The target of `bill_line`'s four-column FK. The four columns ARE the
    -- control cell a bill line must post to.
    CONSTRAINT ux_po_line_cell UNIQUE (po_line_id, po_id, wbs_id, budget_head_id),

    -- Replaces trigger `po_line_project_ownership` (two halves, see header).
    CONSTRAINT fk_po_line_po_project
        FOREIGN KEY (po_id, project_id)
        REFERENCES purchase_order (po_id, project_id) ON UPDATE RESTRICT,
    CONSTRAINT fk_po_line_wbs_project
        FOREIGN KEY (wbs_id, project_id)
        REFERENCES wbs_element (wbs_id, project_id) ON UPDATE RESTRICT,

    -- Replaces trigger `po_line_non_negative`, verbatim in the three columns it
    -- named, and now covering UPDATE. `rate_paise` and `tax_paise` are
    -- deliberately NOT added to it: the trigger constrained three columns, the
    -- frozen contract repeats those three, and widening a value guard past what
    -- either states would reject rows on this migration's author's opinion.
    CONSTRAINT ck_po_line_non_negative CHECK (
        amount_paise >= 0
        AND non_creditable_tax_paise >= 0
        AND freight_paise >= 0
    )
);

CREATE INDEX ix_po_line_po   ON po_line (po_id);
CREATE INDEX ix_po_line_cell ON po_line (wbs_id, budget_head_id);

-- Two PO lines on one purchase order carrying the same Zoho line item id is a
-- duplicated import, not a second line. PARTIAL, so locally-raised lines with
-- no external identity do not collide.
CREATE UNIQUE INDEX ux_po_line_external
    ON po_line (po_id, line_external_id)
    WHERE line_external_id IS NOT NULL;

-- ======================================================================== grn
CREATE TABLE grn (
    grn_id      text PRIMARY KEY,
    grn_number  text NOT NULL,
    po_id       text NOT NULL REFERENCES purchase_order (po_id),
    received_at timestamptz NOT NULL,
    status      text NOT NULL DEFAULT 'Approved',

    -- AUD-C-004: reversal by FLAG. A reversal is a new GRN carrying this true,
    -- never an edit of the original and never a delete.
    is_reversal boolean NOT NULL DEFAULT false,

    -- GAP-2: the §6.1 block the POC never gave this table. `external_id` ports
    -- `grn.zoho_receive_id`.
    external_source        text,
    external_id            text,
    external_last_modified timestamptz,
    payload_sha            text,

    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    version_no  integer NOT NULL DEFAULT 1,

    CONSTRAINT ux_grn_number UNIQUE (grn_number),

    -- Exists only to be an FK target: `fk_grn_line_grn_po` is the half of
    -- trigger `grn_line_po_ownership` that read `g.po_id`.
    CONSTRAINT ux_grn_id_po UNIQUE (grn_id, po_id)
);

CREATE INDEX ix_grn_po ON grn (po_id);

CREATE UNIQUE INDEX ux_grn_external
    ON grn (external_source, external_id)
    WHERE external_id IS NOT NULL;

-- =================================================================== grn_line
-- SIGNED money and SIGNED quantity. See the header: the POC seeds a reversal
-- line at -0.2 / -1,20,000 and a `>= 0` CHECK here rejects the reversal
-- contract itself.
CREATE TABLE grn_line (
    grn_line_id  text PRIMARY KEY,
    grn_id       text NOT NULL REFERENCES grn (grn_id),

    -- Denormalised (§6.2). Bound to the GRN's own po_id by fk_grn_line_grn_po,
    -- and to the PO line's by fk_grn_line_po_line -- which is what makes the
    -- two agree.
    po_id        text NOT NULL,
    po_line_id   text NOT NULL,

    -- `sweeps.py::record_receive_line` supplies exactly these two.
    receive_external_id text,
    line_external_id    text,

    quantity     numeric NOT NULL,
amount_paise bigint NOT NULL,

    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    version_no   integer NOT NULL DEFAULT 1,

    -- Replaces trigger `grn_line_po_ownership`, both halves, on UPDATE too.
    CONSTRAINT fk_grn_line_po_line
        FOREIGN KEY (po_line_id, po_id)
        REFERENCES po_line (po_line_id, po_id) ON UPDATE RESTRICT,
    CONSTRAINT fk_grn_line_grn_po
        FOREIGN KEY (grn_id, po_id)
        REFERENCES grn (grn_id, po_id) ON UPDATE RESTRICT,

    -- Idempotent replay. The sweeps re-walk by design -- a 300-second overlap
    -- and a cycling walk -- so the same receive line arrives more than once and
    -- must not become two receipts.
    --
    -- NULLs are DISTINCT here, which is PostgreSQL's default and is wanted: a
    -- locally-raised receipt line carries neither external id, and every such
    -- line must remain insertable rather than all colliding on one NULL row.
    CONSTRAINT ux_grn_line_external
        UNIQUE (po_line_id, receive_external_id, line_external_id)
);

CREATE INDEX ix_grn_line_grn     ON grn_line (grn_id);
CREATE INDEX ix_grn_line_po_line ON grn_line (po_line_id);

-- ======================================================================= bill
CREATE TABLE bill (
    bill_id      text PRIMARY KEY,
    bill_number  text NOT NULL,

    -- NULLABLE: non-PO bills exist. That nullability is the whole reason for
    -- `project_id` below -- see CONTRACT GAP 4 in the header.
    po_id        text REFERENCES purchase_order (po_id),
    project_id   text NOT NULL REFERENCES project (project_id),

    vendor_name  text NOT NULL,
    bill_date    date NOT NULL,
    status       text NOT NULL DEFAULT 'Approved',

    -- AUD-C-004. Only Approved and Reversal are accounting-effective.
    accounting_status text NOT NULL DEFAULT 'Approved',
    is_reversal  boolean NOT NULL DEFAULT false,
    reverses_bill_id text REFERENCES bill (bill_id),
    doc_type     text NOT NULL DEFAULT 'BILL',

    voided_by    text,
    voided_at    timestamptz,
    void_reason  text,

    external_source        text,
    external_id            text,
    external_last_modified timestamptz,
    payload_sha            text,

    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    version_no   integer NOT NULL DEFAULT 1,

    CONSTRAINT ux_bill_number UNIQUE (bill_number),

    -- Exists only to be an FK target: `fk_bill_line_bill_po` is the half of the
    -- bill_line ownership trigger pair that read `b.po_id`.
    CONSTRAINT ux_bill_id_po UNIQUE (bill_id, po_id),

    -- MATCH SIMPLE (the default): inert while `po_id` is NULL, which is exactly
    -- the non-PO bill this must not obstruct; binding the moment a PO is named,
    -- so a bill can never claim a project its purchase order does not have.
    CONSTRAINT fk_bill_po_project
        FOREIGN KEY (po_id, project_id)
        REFERENCES purchase_order (po_id, project_id) ON UPDATE RESTRICT,

    -- GAP-3, AUD-C-004's four values.
    CONSTRAINT ck_bill_accounting_status CHECK (
        accounting_status IN ('Draft', 'Approved', 'Void', 'Reversal')
    ),
    CONSTRAINT ck_bill_doc_type CHECK (
        doc_type IN ('BILL', 'CREDIT_NOTE', 'DEBIT_NOTE')
    )
);

CREATE INDEX ix_bill_project ON bill (project_id);
CREATE INDEX ix_bill_po ON bill (po_id) WHERE po_id IS NOT NULL;

-- `ux_bill_external` from 002_financial_controls.sql, ported 1:1.
CREATE UNIQUE INDEX ux_bill_external
    ON bill (external_source, external_id)
    WHERE external_id IS NOT NULL;

-- ================================================================== bill_line
CREATE TABLE bill_line (
    bill_line_id text PRIMARY KEY,
    bill_id      text NOT NULL REFERENCES bill (bill_id),

    -- Both NULLABLE. A non-PO bill line names no PO line, and the four-column
    -- FK is then simply not enforced -- exactly what the trigger's
    -- `WHEN NEW.po_line_id IS NOT NULL` guard did.
    po_id        text,
    po_line_id   text,

    wbs_id       text NOT NULL REFERENCES wbs_element (wbs_id),
    budget_head_id text NOT NULL REFERENCES budget_head (budget_head_id),

    description  text,
    quantity     numeric NOT NULL DEFAULT 1,
    -- SIGNED: a credit note's line amounts are negative paise (§2.4).
amount_paise bigint NOT NULL,
non_creditable_tax_paise bigint NOT NULL DEFAULT 0,
freight_paise bigint NOT NULL DEFAULT 0,

    -- Ports `bill_line.zoho_purchaseorder_item_id`, which held the PO LINE's
    -- external id, not this line's. Named for what it holds.
    po_line_external_id text,

    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    version_no   integer NOT NULL DEFAULT 1,

    -- Replaces triggers `bill_line_po_ownership_insert` / `_update`, and adds
    -- what neither covered: ON UPDATE RESTRICT stops the PO LINE being
    -- re-pointed underneath a bill that has already posted against it.
    CONSTRAINT fk_bill_line_po_line_cell
        FOREIGN KEY (po_line_id, po_id, wbs_id, budget_head_id)
        REFERENCES po_line (po_line_id, po_id, wbs_id, budget_head_id)
        ON UPDATE RESTRICT,

    -- ...and the half the trigger enforced by joining `bill b ON b.bill_id`:
    -- the PO the LINE claims must be the PO the BILL is raised against.
    CONSTRAINT fk_bill_line_bill_po
        FOREIGN KEY (bill_id, po_id)
        REFERENCES bill (bill_id, po_id) ON UPDATE RESTRICT,

    -- Closes the hole MATCH SIMPLE would otherwise leave. A composite FK is not
    -- checked at all when ANY of its columns is NULL, so `po_line_id` set with
    -- `po_id` NULL would name a PO line and be enforced against nothing --
    -- a bill line pointing at another purchase order's line, admitted silently.
    CONSTRAINT ck_bill_line_po_id_accompanies_po_line CHECK (
        po_line_id IS NULL OR po_id IS NOT NULL
    )
);

CREATE INDEX ix_bill_line_bill ON bill_line (bill_id);
CREATE INDEX ix_bill_line_cell ON bill_line (wbs_id, budget_head_id);
CREATE INDEX ix_bill_line_po_line
    ON bill_line (po_line_id) WHERE po_line_id IS NOT NULL;

-- ============================================ the derived purchase-request total
-- GAP-1's reconciliation between the new line grain and the header the existing
-- financial controls read.
--
-- WHY THIS IS A TRIGGER AND NOT A CHECK. The frozen contract calls for a "named
-- CHECK-backed trigger". A CHECK constraint cannot be that: PostgreSQL CHECK
-- expressions may not query another table, so "this header equals the sum of
-- its lines" is not expressible as one, and declaring it would either be
-- rejected or -- worse, with a function wrapper -- be evaluated only on write
-- to the header and never on write to a line. The equality is MAINTAINED here
-- instead, which is stronger than checking it: there is no window in which the
-- header can disagree with its lines. `ck_purchase_request_amount_nonneg` is
-- the named CHECK that remains, and it constrains the sign.
--
-- Runs with the caller's own privileges and under the caller's RLS, on purpose:
-- adding a line to a purchase request you cannot see must fail, and it does,
-- at `purchase_request_scope`'s WITH CHECK.
-- `TG_OP` is branched on explicitly rather than written as
-- `COALESCE(NEW.pr_id, OLD.pr_id)`. In a row-level DELETE trigger PL/pgSQL's
-- `NEW` is an unassigned record, and reading a field of it is an error rather
-- than a NULL, so the COALESCE spelling looks defensive and raises the moment
-- a line is removed.
--
-- The UPDATE branch refreshes BOTH headers, because `pr_id` is updatable: a
-- line moved from one request to another leaves the request it LEFT holding a
-- total that still counts it. Refreshing only `NEW.pr_id` would make the
-- source header silently overstate, which is the direction that lets a budget
-- check pass on money that is no longer requested.
--
-- `SUM()` over `bigint` returns `numeric` in PostgreSQL, so the cast back to
-- `bigint` is mandatory, not decoration: without it the assignment would round
-- through a numeric and the "integer paise, never numeric" rule would be
-- broken by the very trigger that maintains the total.
CREATE OR REPLACE FUNCTION capex_purchase_request_total_refresh()
RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    affected text[];
    target text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        affected := ARRAY[OLD.pr_id];
    ELSIF TG_OP = 'UPDATE' THEN
        affected := ARRAY[NEW.pr_id, OLD.pr_id];
    ELSE
        affected := ARRAY[NEW.pr_id];
    END IF;

    FOREACH target IN ARRAY affected LOOP
        UPDATE purchase_request pr
           SET amount_paise = COALESCE(
                   (SELECT SUM(l.amount_paise)::bigint
                      FROM pr_line l WHERE l.pr_id = target), 0),
               updated_at = now()
         WHERE pr.pr_id = target
           AND pr.amount_paise IS DISTINCT FROM COALESCE(
                   (SELECT SUM(l.amount_paise)::bigint
                      FROM pr_line l WHERE l.pr_id = target), 0);
    END LOOP;
    RETURN NULL;
END;
$$;

-- Three triggers rather than one FOR EACH STATEMENT: the row-level form gives
-- the function a NEW/OLD to read `pr_id` from, and a statement-level trigger
-- would have to re-derive the affected headers from a transition table for no
-- gain at these row counts.
CREATE TRIGGER pr_line_total_after_insert AFTER INSERT ON pr_line
    FOR EACH ROW EXECUTE FUNCTION capex_purchase_request_total_refresh();
CREATE TRIGGER pr_line_total_after_update AFTER UPDATE ON pr_line
    FOR EACH ROW EXECUTE FUNCTION capex_purchase_request_total_refresh();
CREATE TRIGGER pr_line_total_after_delete AFTER DELETE ON pr_line
    FOR EACH ROW EXECUTE FUNCTION capex_purchase_request_total_refresh();

-- ========================================================= row-level security
-- ENABLE **and** FORCE on every one of the eight. FORCE is the half most easily
-- dropped and the one that is invisible from `pg_policies`: without it the
-- table's OWNER -- in production the deploy identity that ran the migrations,
-- which is not a superuser -- bypasses every policy silently. (Superusers
-- bypass RLS unconditionally and FORCE does not change that, which is why every
-- test of these policies must go through `SET LOCAL ROLE capex_app`.)

ALTER TABLE purchase_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchase_request FORCE ROW LEVEL SECURITY;
CREATE POLICY purchase_request_scope ON purchase_request
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = purchase_request.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = purchase_request.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

-- `pr_line.project_id` is not an independent claim: `fk_pr_line_pr_project`
-- forces it to equal the header's, so reaching `project` through it reaches the
-- parent's project by construction. One join, FK-guaranteed -- unlike
-- `budget_version_cell` in 006, which needs both of its paths precisely because
-- 003 declares no FK on its `wbs_id`.
ALTER TABLE pr_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE pr_line FORCE ROW LEVEL SECURITY;
CREATE POLICY pr_line_scope ON pr_line
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = pr_line.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = pr_line.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

ALTER TABLE purchase_order ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchase_order FORCE ROW LEVEL SECURITY;
CREATE POLICY purchase_order_scope ON purchase_order
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = purchase_order.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = purchase_order.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

-- As pr_line: `fk_po_line_po_project` binds this column to the order's.
ALTER TABLE po_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE po_line FORCE ROW LEVEL SECURITY;
CREATE POLICY po_line_scope ON po_line
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = po_line.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = po_line.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

-- A goods receipt carries no project of its own; it reaches one through the
-- purchase order it receives against, which is NOT NULL.
ALTER TABLE grn ENABLE ROW LEVEL SECURITY;
ALTER TABLE grn FORCE ROW LEVEL SECURITY;
CREATE POLICY grn_scope ON grn
    USING (
        EXISTS (
            SELECT 1 FROM purchase_order po
            JOIN project p ON p.project_id = po.project_id
            WHERE po.po_id = grn.po_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM purchase_order po
            JOIN project p ON p.project_id = po.project_id
            WHERE po.po_id = grn.po_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

-- `grn_line.po_id` is tied to its GRN's by `fk_grn_line_grn_po`, so this reads
-- the parent's purchase order however it is spelled.
ALTER TABLE grn_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE grn_line FORCE ROW LEVEL SECURITY;
CREATE POLICY grn_line_scope ON grn_line
    USING (
        EXISTS (
            SELECT 1 FROM purchase_order po
            JOIN project p ON p.project_id = po.project_id
            WHERE po.po_id = grn_line.po_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM purchase_order po
            JOIN project p ON p.project_id = po.project_id
            WHERE po.po_id = grn_line.po_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

ALTER TABLE bill ENABLE ROW LEVEL SECURITY;
ALTER TABLE bill FORCE ROW LEVEL SECURITY;
CREATE POLICY bill_scope ON bill
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = bill.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = bill.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

-- TWO-LEGGED, and both legs required -- the `budget_version_cell` shape, for
-- the same reason it has that shape. A bill line reaches a project two ways:
--
--   * `bill_id` -> `bill.project_id`   (FK-enforced)
--   * `wbs_id`  -> `wbs_element.project_id`
--
-- and NOTHING ties the second to the first when `po_line_id` is NULL: a non-PO
-- bill line names any WBS it likes, and the four-column FK is not checked. The
-- version path alone would let such a line ride in on its bill's visibility
-- while posting to another project's control cell. `AND`, therefore, and each
-- clause an `EXISTS`, so a `wbs_id` matching no `wbs_element` denies rather
-- than waives.
ALTER TABLE bill_line ENABLE ROW LEVEL SECURITY;
ALTER TABLE bill_line FORCE ROW LEVEL SECURITY;
CREATE POLICY bill_line_scope ON bill_line
    USING (
        EXISTS (
            SELECT 1 FROM bill b
            JOIN project p ON p.project_id = b.project_id
            WHERE b.bill_id = bill_line.bill_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
        AND EXISTS (
            SELECT 1 FROM wbs_element we
            JOIN project p ON p.project_id = we.project_id
            WHERE we.wbs_id = bill_line.wbs_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM bill b
            JOIN project p ON p.project_id = b.project_id
            WHERE b.bill_id = bill_line.bill_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
        AND EXISTS (
            SELECT 1 FROM wbs_element we
            JOIN project p ON p.project_id = we.project_id
            WHERE we.wbs_id = bill_line.wbs_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

-- ================================================== privileges for capex_app
-- Stated explicitly rather than relied upon: 004's ALTER DEFAULT PRIVILEGES
-- attaches to the role that ISSUED it, so a deployment whose 013 is applied by
-- a different identity than its 004 would otherwise leave the application
-- unable to read its own new tables -- a failure that appears in exactly one
-- environment.
GRANT SELECT, INSERT, UPDATE ON
    purchase_request, pr_line, purchase_order, po_line,
    grn, grn_line, bill, bill_line
    TO capex_app;

-- ...and DELETE taken away, in the same migration, rather than trusting a
-- future caller to remember. This is not belt-and-braces for the GRANT above:
-- 004's `ALTER DEFAULT PRIVILEGES ... GRANT SELECT, INSERT, UPDATE, DELETE ON
-- TABLES TO capex_app` already handed capex_app DELETE on all eight the instant
-- each of them came into existence. Omitting DELETE from the GRANT above
-- removes nothing.
-- This REVOKE is what actually makes these documents undeletable by the
-- application, exactly as 004 does for audit_log and 008 for approval_action.
--
-- Reversal is a NEW row with is_reversal = true. Never an edit, never a delete.
REVOKE DELETE ON
    purchase_request, pr_line, purchase_order, po_line,
    grn, grn_line, bill, bill_line
    FROM capex_app;

COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- WHAT REVERTING COSTS.
--   --
--   -- Dropping these eight tables DESTROYS every purchase request, order,
--   -- receipt and bill in the database. There is no soft delete on a financial
--   -- document (§6.1) and no archive: the rows are gone. Take a dump first.
--   --
--   -- It also puts the product back where 001..012 left it -- with no
--   -- procurement chain at all -- so `/api/integrations/reconciliation` refuses
--   -- again, `sweeps.py` quarantines every receive line as unattributed
--   -- (`GRN_LINE_UNATTRIBUTED`, held at full value), and every one of those
--   -- exceptions blocks capitalisation until someone triages it. That is loud
--   -- and it is the safe direction, but it is not free: reverting mid-sweep
--   -- leaves a backlog of exceptions that re-applying 013 does not clear.
--   --
--   -- The DROPs are ordered by dependency (children before parents) rather
--   -- than using CASCADE, so a table this block has forgotten raises instead
--   -- of being silently taken with something else.
--   DROP POLICY IF EXISTS bill_line_scope ON bill_line;
--   DROP POLICY IF EXISTS bill_scope ON bill;
--   DROP POLICY IF EXISTS grn_line_scope ON grn_line;
--   DROP POLICY IF EXISTS grn_scope ON grn;
--   DROP POLICY IF EXISTS po_line_scope ON po_line;
--   DROP POLICY IF EXISTS purchase_order_scope ON purchase_order;
--   DROP POLICY IF EXISTS pr_line_scope ON pr_line;
--   DROP POLICY IF EXISTS purchase_request_scope ON purchase_request;
--   ALTER TABLE bill_line NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE bill_line DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE bill NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE bill DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE grn_line NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE grn_line DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE grn NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE grn DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE po_line NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE po_line DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE purchase_order NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE purchase_order DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE pr_line NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE pr_line DISABLE ROW LEVEL SECURITY;
--   ALTER TABLE purchase_request NO FORCE ROW LEVEL SECURITY;
--   ALTER TABLE purchase_request DISABLE ROW LEVEL SECURITY;
--   DROP TRIGGER IF EXISTS pr_line_total_after_delete ON pr_line;
--   DROP TRIGGER IF EXISTS pr_line_total_after_update ON pr_line;
--   DROP TRIGGER IF EXISTS pr_line_total_after_insert ON pr_line;
--   DROP TABLE IF EXISTS bill_line;
--   DROP TABLE IF EXISTS bill;
--   DROP TABLE IF EXISTS grn_line;
--   DROP TABLE IF EXISTS grn;
--   DROP TABLE IF EXISTS po_line;
--   DROP TABLE IF EXISTS purchase_order;
--   DROP TABLE IF EXISTS pr_line;
--   DROP TABLE IF EXISTS purchase_request;
--   DROP FUNCTION IF EXISTS capex_purchase_request_total_refresh();
--   DELETE FROM schema_migrations WHERE version = '013';
--   COMMIT;
