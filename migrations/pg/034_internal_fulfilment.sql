-- 034_internal_fulfilment.sql
-- Internal material fulfilment and captive consumption: a purchase-request
-- line may be met from the company's own stock instead of, or as well as, by
-- an external purchase, and the budget follows it without being counted twice.
--
-- THE DECISION THIS TRANSCRIBES (product owner, 2026-09-13)
-- ========================================================
--
-- Every APPROVED purchase-request line carries a fulfilment decision:
--
--   EXTERNAL_PURCHASE  -- the whole line converts to a purchase order (today's
--                         only path, now stated rather than assumed);
--   INTERNAL_TRANSFER  -- the whole line is met from stores; nothing converts;
--   SPLIT_FULFILMENT   -- part external, part internal; the external part
--                         alone converts, the internal part is requested from
--                         stores.
--
-- The internal part is an INTERNAL MATERIAL REQUEST (IMR) with the lifecycle
-- REQUESTED -> APPROVED -> ALLOCATED -> PARTIALLY_ISSUED / ISSUED
--           -> PARTIALLY_CONSUMED / CONSUMED, or RETURNED, or CANCELLED,
-- and every quantity that moves is an append-only INTERNAL MATERIAL MOVEMENT.
--
-- THE MONEY, AND WHY IT NEVER DOUBLE-COUNTS
-- ========================================
--
-- Two new derived columns on `budget_ledger_cell`, both maintained ONLY by
-- `procurement_services._RECOMPUTE_DERIVED_SQL` (the one statement that
-- derives a ledger column -- `tests/test_pg_reservations.py` pins that), both
-- added to exposure beside commitment, actual and pr_reserved:
--
--   internal_allocation_paise  -- INTERNAL_ALLOCATION_COMMITMENT: stock set
--                                 aside for the project and not yet issued.
--                                 Per IMR: ALLOCATE - ISSUE - CANCEL, floored
--                                 at zero, then summed.
--   internal_consumption_paise -- INTERNAL_CONSUMPTION_ACTUAL (CWIP): stock
--                                 issued to the project and not returned.
--                                 Per IMR: ISSUE - RETURN.
--
-- An ISSUE therefore MOVES money from allocation to consumption and changes
-- exposure by nothing; a RETURN or a CANCEL RELEASES it; a TRANSFER between
-- two stores and a CONSUME confirmation move no money at all. A warehouse-to-
-- warehouse transfer is not consumption.
--
-- The reservation side is `pr_reservation.internal_moved_paise`: when an IMR
-- is allocated, the part of the request's live hold that covers it is MOVED
-- into the allocation rather than held twice -- `pr_reserved_paise` now
-- derives from `amount_paise - internal_moved_paise` -- so exposure rises by
-- only the part the hold did not already cover, and that part is checked
-- against availability exactly as a purchase order is. A cancel or a return
-- while the hold is still Reserved moves it back, so the request keeps its
-- hold for whatever still has to be fulfilled.
--
-- `pr_line_fulfilment` splits the LINE's quantity and paise into external and
-- internal portions that sum EXACTLY to the line (CHECK-enforced); the
-- conversion to a purchase order reads the EXTERNAL portion and refuses a
-- request with no external quantity left.
--
-- VALUATION AND MAPPING ARE VISIBLE FAILURES, NEVER SILENT ZEROS
-- ==============================================================
--
-- `internal_material_request.unit_rate_paise` is NULL until the ERP stock
-- valuation (through the provider boundary in
-- `integration/inventory_provider.py`) or a reasoned manual valuation supplies
-- it; the CHECK ties NULL to `valuation_source = 'MISSING'`, an allocation
-- refuses while it is missing, and the miss is a reconciliation exception,
-- INTERNAL_VALUATION_MISSING. A line whose project x WBS x budget-head
-- mapping resolves to no budget-owning cell is INTERNAL_MAPPING_MISSING and
-- the request cannot be approved until it resolves. Both kinds join
-- `ck_reconciliation_exception_kind` (eleven members, same name) and the C18
-- registry.
--
-- Nothing here edits a byte of 001..033. Additive: three tables, three
-- columns, the kind CHECK dropped and re-added under the SAME name, a
-- numbering series, RLS in 014's shape, grants named for `capex_app`.

BEGIN;

-- ---------------------------------------------------------------- the ledger
ALTER TABLE budget_ledger_cell
    ADD COLUMN internal_allocation_paise  bigint NOT NULL DEFAULT 0,
    ADD COLUMN internal_consumption_paise bigint NOT NULL DEFAULT 0;

ALTER TABLE budget_ledger_cell
    ADD CONSTRAINT ck_ledger_internal_allocation_nonneg
        CHECK (internal_allocation_paise >= 0),
    ADD CONSTRAINT ck_ledger_internal_consumption_nonneg
        CHECK (internal_consumption_paise >= 0);

COMMENT ON COLUMN budget_ledger_cell.internal_allocation_paise IS
    'INTERNAL_ALLOCATION_COMMITMENT: stock allocated to the cell''s project and not yet issued, in base paise. Derived by procurement_services._RECOMPUTE_DERIVED_SQL from internal_material_movement (ALLOCATE - ISSUE - CANCEL per request); part of exposure (034).';
COMMENT ON COLUMN budget_ledger_cell.internal_consumption_paise IS
    'INTERNAL_CONSUMPTION_ACTUAL (CWIP): stock issued to the project and not returned, in base paise. Derived from internal_material_movement (ISSUE - RETURN per request); part of exposure (034).';

-- ----------------------------------------------------------- the reservation
ALTER TABLE pr_reservation
    ADD COLUMN internal_moved_paise bigint NOT NULL DEFAULT 0;

ALTER TABLE pr_reservation
    ADD CONSTRAINT ck_pr_reservation_internal_moved_within_hold
        CHECK (internal_moved_paise >= 0 AND internal_moved_paise <= amount_paise);

COMMENT ON COLUMN pr_reservation.internal_moved_paise IS
    'The part of this hold moved into an internal allocation (034). pr_reserved_paise derives from amount_paise - internal_moved_paise while the hold is Reserved, so the same rupee is never both held and allocated.';

-- --------------------------------------------------- the fulfilment decision
CREATE TABLE pr_line_fulfilment (
    pr_line_id             text PRIMARY KEY REFERENCES pr_line (pr_line_id),
    pr_id                  text NOT NULL REFERENCES purchase_request (pr_id),
    -- Denormalised for the RLS policy, exactly as pr_line.project_id is.
    project_id             text NOT NULL REFERENCES project (project_id),
    mode                   text NOT NULL,
    line_quantity          numeric NOT NULL,
    external_quantity      numeric NOT NULL,
    internal_quantity      numeric NOT NULL,
    line_amount_paise      bigint NOT NULL,
    external_amount_paise  bigint NOT NULL,
    internal_amount_paise  bigint NOT NULL,
    reason                 text,
    decided_at             timestamptz NOT NULL DEFAULT now(),
    decided_by             text NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    created_by             text NOT NULL,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    updated_by             text NOT NULL,
    version_no             integer NOT NULL DEFAULT 1,

    CONSTRAINT ck_pr_line_fulfilment_mode CHECK (
        mode IN ('EXTERNAL_PURCHASE', 'INTERNAL_TRANSFER', 'SPLIT_FULFILMENT')),
    CONSTRAINT ck_pr_line_fulfilment_quantities_nonneg CHECK (
        line_quantity > 0 AND external_quantity >= 0 AND internal_quantity >= 0),
    -- The split never exceeds, and never falls short of, the approved line.
    CONSTRAINT ck_pr_line_fulfilment_quantity_split CHECK (
        external_quantity + internal_quantity = line_quantity),
    CONSTRAINT ck_pr_line_fulfilment_amounts_nonneg CHECK (
        line_amount_paise >= 0 AND external_amount_paise >= 0
        AND internal_amount_paise >= 0),
    CONSTRAINT ck_pr_line_fulfilment_amount_split CHECK (
        external_amount_paise + internal_amount_paise = line_amount_paise),
    CONSTRAINT ck_pr_line_fulfilment_mode_matches_split CHECK (
        (mode = 'EXTERNAL_PURCHASE' AND internal_quantity = 0)
        OR (mode = 'INTERNAL_TRANSFER' AND external_quantity = 0)
        OR (mode = 'SPLIT_FULFILMENT' AND internal_quantity > 0
            AND external_quantity > 0))
);

CREATE INDEX ix_pr_line_fulfilment_pr ON pr_line_fulfilment (pr_id);

COMMENT ON TABLE pr_line_fulfilment IS
    'The fulfilment decision on one APPROVED purchase-request line (034): how much of the line converts to a purchase order and how much is met from stores. Quantities and paise split exactly; the conversion reads the external portion.';

-- ------------------------------------------------ the internal material request
CREATE TABLE internal_material_request (
    imr_id               text PRIMARY KEY,
    imr_number           text NOT NULL,
    pr_id                text NOT NULL REFERENCES purchase_request (pr_id),
    pr_line_id           text NOT NULL REFERENCES pr_line (pr_line_id),
    project_id           text NOT NULL REFERENCES project (project_id),
    wbs_id               text NOT NULL,
    budget_head_id       text NOT NULL REFERENCES budget_head (budget_head_id),
    item_external_id     text,
    item_description     text,
    from_location_id     text REFERENCES location (location_id),
    to_location_id       text REFERENCES location (location_id),

    requested_quantity   numeric NOT NULL,
    approved_quantity    numeric,
    allocated_quantity   numeric NOT NULL DEFAULT 0,
    issued_quantity      numeric NOT NULL DEFAULT 0,
    returned_quantity    numeric NOT NULL DEFAULT 0,
    consumed_quantity    numeric NOT NULL DEFAULT 0,

    -- Valuation: NULL until the ERP stock valuation or a reasoned manual
    -- figure supplies it. Never zero by default.
    unit_rate_paise      bigint,
    valuation_source     text NOT NULL DEFAULT 'MISSING',
    valuation_reference  text,
    valuation_note       text,

    status               text NOT NULL DEFAULT 'REQUESTED',
    mapping_ok           boolean NOT NULL DEFAULT true,
    reason               text,
    requested_by         text NOT NULL,
    approved_by          text,
    approved_at          timestamptz,
    cancelled_at         timestamptz,
    cancel_reason        text,

    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text NOT NULL,
    updated_at           timestamptz NOT NULL DEFAULT now(),
    updated_by           text NOT NULL,
    version_no           integer NOT NULL DEFAULT 1,

    CONSTRAINT fk_imr_wbs_project
        FOREIGN KEY (wbs_id, project_id)
        REFERENCES wbs_element (wbs_id, project_id) ON UPDATE RESTRICT,
    CONSTRAINT ck_imr_status CHECK (
        status IN ('REQUESTED', 'APPROVED', 'ALLOCATED', 'PARTIALLY_ISSUED',
                   'ISSUED', 'PARTIALLY_CONSUMED', 'CONSUMED', 'RETURNED',
                   'CANCELLED')),
    CONSTRAINT ck_imr_quantities CHECK (
        requested_quantity > 0
        AND (approved_quantity IS NULL OR approved_quantity > 0)
        AND allocated_quantity >= 0 AND issued_quantity >= 0
        AND returned_quantity >= 0 AND consumed_quantity >= 0
        AND issued_quantity <= allocated_quantity
        AND returned_quantity + consumed_quantity <= issued_quantity),
    CONSTRAINT ck_imr_valuation_source CHECK (
        valuation_source IN ('ERP_STOCK', 'MANUAL', 'MISSING')),
    -- A missing valuation is NULL and says so; a present one is positive.
    CONSTRAINT ck_imr_valuation_present_or_missing CHECK (
        (valuation_source = 'MISSING') = (unit_rate_paise IS NULL)),
    CONSTRAINT ck_imr_rate_positive CHECK (
        unit_rate_paise IS NULL OR unit_rate_paise > 0),
    CONSTRAINT ck_imr_cancel_is_reasoned CHECK (
        status <> 'CANCELLED' OR (cancel_reason IS NOT NULL AND cancelled_at IS NOT NULL))
);

CREATE UNIQUE INDEX ux_imr_number ON internal_material_request (imr_number);
-- One LIVE request per purchase-request line: a second request for the same
-- line while one is open would allocate the same material twice.
CREATE UNIQUE INDEX ux_imr_live_per_line ON internal_material_request (pr_line_id)
    WHERE status NOT IN ('CONSUMED', 'RETURNED', 'CANCELLED');
CREATE INDEX ix_imr_cell ON internal_material_request (wbs_id, budget_head_id);
CREATE INDEX ix_imr_project_status ON internal_material_request (project_id, status);

COMMENT ON TABLE internal_material_request IS
    'A request to meet (part of) a purchase-request line from the company''s own stock (034). Quantities are the running totals of internal_material_movement; the money is derived on the ledger cell from the movements, never stored here.';

-- --------------------------------------------------------------- movements
CREATE TABLE internal_material_movement (
    movement_id      text PRIMARY KEY,
    imr_id           text NOT NULL REFERENCES internal_material_request (imr_id),
    -- Denormalised from the request for the ledger derivation and RLS.
    project_id       text NOT NULL REFERENCES project (project_id),
    wbs_id           text NOT NULL,
    budget_head_id   text NOT NULL REFERENCES budget_head (budget_head_id),
    kind             text NOT NULL,
    quantity         numeric NOT NULL,
    amount_paise     bigint NOT NULL,
    from_location_id text REFERENCES location (location_id),
    to_location_id   text REFERENCES location (location_id),
    idempotency_key  text NOT NULL,
    reference        text,
    note             text,
    occurred_at      timestamptz NOT NULL DEFAULT now(),
    correlation_id   text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    created_by       text NOT NULL,

    CONSTRAINT ck_imm_kind CHECK (
        kind IN ('ALLOCATE', 'ISSUE', 'RETURN', 'CONSUME', 'TRANSFER', 'CANCEL')),
    CONSTRAINT ck_imm_quantity_positive CHECK (quantity > 0),
    CONSTRAINT ck_imm_amount_nonneg CHECK (amount_paise >= 0),
    -- A transfer between stores and a consumption confirmation move no money.
    CONSTRAINT ck_imm_no_money_on_transfer_or_consume CHECK (
        kind NOT IN ('TRANSFER', 'CONSUME') OR amount_paise = 0),
    CONSTRAINT ck_imm_transfer_names_both_stores CHECK (
        kind <> 'TRANSFER'
        OR (from_location_id IS NOT NULL AND to_location_id IS NOT NULL
            AND from_location_id <> to_location_id)),
    -- The same intent replayed lands on the same row, once.
    CONSTRAINT ux_imm_idempotent UNIQUE (imr_id, idempotency_key)
);

CREATE INDEX ix_imm_cell_kind ON internal_material_movement (wbs_id, budget_head_id, kind);
CREATE INDEX ix_imm_imr ON internal_material_movement (imr_id, occurred_at);

COMMENT ON TABLE internal_material_movement IS
    'Append-only: every quantity that moves on an internal material request (034). ALLOCATE and ISSUE and RETURN and CANCEL carry base paise at the request''s valuation; TRANSFER (store to store) and CONSUME (confirmation) carry none.';

-- -------------------------------------------------------------- numbering
INSERT INTO numbering_series
    (series_id, code, prefix, suffix, pad_width, reset_policy, description,
     created_by, updated_by)
VALUES
    ('NS-IMR', 'INTERNAL_MATERIAL_REQUEST', 'IMR-', '', 4, 'YEARLY',
     'Internal material requests (034). period_key is the fiscal year.',
     'SYSTEM', 'SYSTEM');

-- -------------------------------------------------------- exception kinds
ALTER TABLE reconciliation_exception
    DROP CONSTRAINT ck_reconciliation_exception_kind;

ALTER TABLE reconciliation_exception
    ADD CONSTRAINT ck_reconciliation_exception_kind CHECK (
        kind IN ('GRN_LINE_UNATTRIBUTED', 'CONTROL_TOTAL_MISMATCH',
                 'LATE_ARRIVAL_CLOSED_PERIOD', 'UNMAPPED_EXTERNAL_STATUS',
                 'UNSANCTIONED_COMMITMENT', 'FOREIGN_CURRENCY_BASIS_MISSING',
                 'ADOPTION_DIMENSION_INVALID', 'ADOPTION_DIMENSION_CONFLICT',
                 'BILL_EXCEEDS_RECEIVE', 'INTERNAL_MAPPING_MISSING',
                 'INTERNAL_VALUATION_MISSING')
    );

COMMENT ON CONSTRAINT ck_reconciliation_exception_kind ON reconciliation_exception IS
    'C18''s exception kinds: the five frozen in 011, FOREIGN_CURRENCY_BASIS_MISSING (030), ADOPTION_DIMENSION_INVALID / ADOPTION_DIMENSION_CONFLICT (032), BILL_EXCEEDS_RECEIVE (033), and INTERNAL_MAPPING_MISSING / INTERNAL_VALUATION_MISSING (034): an internal material request whose project x WBS x budget-head mapping resolves to no budget-owning cell, and one whose stock valuation neither the ERP nor a reasoned manual entry has supplied. Both raised on the request; never a silent zero.';

-- ------------------------------------------------------------- privileges
-- 004's ALTER DEFAULT PRIVILEGES attaches to the role that issued it, so the
-- grants are named. DELETE is withheld on all three: a decision, a request and
-- a movement are the record of what happened to budget and to stock.
GRANT SELECT, INSERT, UPDATE ON pr_line_fulfilment TO capex_app;
GRANT SELECT, INSERT, UPDATE ON internal_material_request TO capex_app;
GRANT SELECT, INSERT ON internal_material_movement TO capex_app;
REVOKE DELETE ON pr_line_fulfilment FROM capex_app;
REVOKE DELETE ON internal_material_request FROM capex_app;
REVOKE UPDATE, DELETE ON internal_material_movement FROM capex_app;

-- ------------------------------------------------------------------- RLS
-- 014's shape: the row's project supplies all four dimensions.
ALTER TABLE pr_line_fulfilment ENABLE ROW LEVEL SECURITY;
ALTER TABLE pr_line_fulfilment FORCE ROW LEVEL SECURITY;
CREATE POLICY pr_line_fulfilment_scope ON pr_line_fulfilment
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = pr_line_fulfilment.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = pr_line_fulfilment.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

ALTER TABLE internal_material_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE internal_material_request FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_material_request_scope ON internal_material_request
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = internal_material_request.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = internal_material_request.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

ALTER TABLE internal_material_movement ENABLE ROW LEVEL SECURITY;
ALTER TABLE internal_material_movement FORCE ROW LEVEL SECURITY;
CREATE POLICY internal_material_movement_scope ON internal_material_movement
    USING (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = internal_material_movement.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM project p
            WHERE p.project_id = internal_material_movement.project_id
              AND capex_scope_permits(p.entity_id, p.plant_id,
                                      p.location_id, p.project_id)
        )
    );

-- Migration 034 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- Every Open INTERNAL_* row is a request the ledger HAS booked; review
--   -- each before deleting.
--   DELETE FROM reconciliation_exception
--       WHERE kind IN ('INTERNAL_MAPPING_MISSING', 'INTERNAL_VALUATION_MISSING');
--   ALTER TABLE reconciliation_exception
--       DROP CONSTRAINT ck_reconciliation_exception_kind;
--   ALTER TABLE reconciliation_exception
--       ADD CONSTRAINT ck_reconciliation_exception_kind CHECK (
--           kind IN ('GRN_LINE_UNATTRIBUTED', 'CONTROL_TOTAL_MISMATCH',
--                    'LATE_ARRIVAL_CLOSED_PERIOD', 'UNMAPPED_EXTERNAL_STATUS',
--                    'UNSANCTIONED_COMMITMENT', 'FOREIGN_CURRENCY_BASIS_MISSING',
--                    'ADOPTION_DIMENSION_INVALID', 'ADOPTION_DIMENSION_CONFLICT',
--                    'BILL_EXCEEDS_RECEIVE'));
--   DROP TABLE IF EXISTS internal_material_movement;
--   DROP TABLE IF EXISTS internal_material_request;
--   DROP TABLE IF EXISTS pr_line_fulfilment;
--   DELETE FROM numbering_issued WHERE series_id = 'NS-IMR';
--   DELETE FROM numbering_counter WHERE series_id = 'NS-IMR';
--   DELETE FROM numbering_series WHERE series_id = 'NS-IMR';
--   ALTER TABLE pr_reservation
--       DROP CONSTRAINT IF EXISTS ck_pr_reservation_internal_moved_within_hold,
--       DROP COLUMN IF EXISTS internal_moved_paise;
--   ALTER TABLE budget_ledger_cell
--       DROP CONSTRAINT IF EXISTS ck_ledger_internal_consumption_nonneg,
--       DROP CONSTRAINT IF EXISTS ck_ledger_internal_allocation_nonneg,
--       DROP COLUMN IF EXISTS internal_consumption_paise,
--       DROP COLUMN IF EXISTS internal_allocation_paise;
--   DELETE FROM schema_migrations WHERE version = '034';
--   COMMIT;
