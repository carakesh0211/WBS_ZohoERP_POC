-- seed_parts/005_masters.sql
-- Demo item/vendor masters, custom-field definitions and numbering series
-- for 005_master_data.sql. Loaded by the lead's seed loader AFTER
-- seed_demo.sql (see migrations/pg/seed_parts/README.md) -- this file does
-- not itself gate on the demo-profile / disposable-database checks
-- app/backend/pg/seed.py enforces; it relies on being reached only through
-- that loader.
--
-- Deliberately demonstrates every `source` value, a duplicate PAIR (flagged,
-- never merged), and an inactive row, per docs/WAVE2_CONTRACTS.md and this
-- stream's brief:
--   * item source coverage:   LOCAL (ITM-DEMO-01), IMPORT (ITM-DEMO-02),
--                              ZOHO/MOCK (ITM-DEMO-03, inactive)
--   * vendor source coverage: LOCAL (VEN-DEMO-01), IMPORT (VEN-DEMO-02),
--                              ZOHO/MOCK (VEN-DEMO-03)
--   * duplicate pair:         ITM-DEMO-01 / ITM-DEMO-02 share a normalised
--                              name ("Steel Rod 12mm" vs "Steel Rod - 12 MM");
--                              VEN-DEMO-01 / VEN-DEMO-02 share one too
--   * inactive row:           ITM-DEMO-03 (deactivated, not deleted)
--   * masking demonstration:  VEN-DEMO-01 carries both gst_no and pan_no
--
-- gst_no/pan_no below are INVENTED, plausibly-shaped demo values (they pass
-- this migration's format CHECKs) -- never a real GSTIN/PAN, and never any
-- real client's data (per this stream's boundaries: no real client data,
-- ever). No source_of_truth_status here is ever LIVE or VERIFIED -- see
-- .claude/skills/wbs-full-app-builder/references/zoho-boundaries.md: there
-- is no live Zoho connection in this wave, so nothing can honestly claim
-- either.

-- ------------------------------------------------------------- numbering
INSERT INTO numbering_series
    (series_id, code, prefix, suffix, pad_width, reset_policy, description,
     created_by, updated_by)
VALUES
    ('NS-ITEM', 'ITEM', 'ITM-', '', 6, 'NEVER',
     'Item master auto-numbering', 'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('NS-VENDOR', 'VENDOR', 'VEN-', '', 6, 'NEVER',
     'Vendor master auto-numbering', 'SEED-SCRIPT', 'SEED-SCRIPT');

-- ------------------------------------------------------------------ items
INSERT INTO item_master
    (item_id, code, name, uom, category, hsn_code, description,
     source, external_source, external_id, external_last_modified, payload_sha,
     source_of_truth_status, mapping_status, is_active, created_by, updated_by)
VALUES
    ('ITM-DEMO-01', 'STL-ROD-12', 'Steel Rod 12mm', 'MT', 'Raw Material', '7214',
     'TMT reinforcement bar, 12mm diameter, Fe500 grade.',
     'LOCAL', NULL, NULL, NULL, NULL, 'LOCAL', 'UNMAPPED', true,
     'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('ITM-DEMO-02', 'STL-ROD-12-ALT', 'Steel Rod - 12 MM', 'MT', 'Raw Material', '7214',
     'Imported catalogue entry for the same physical item as STL-ROD-12, '
     'entered independently -- exactly the case duplicate detection exists for.',
     'IMPORT', NULL, NULL, NULL, NULL, 'UNVERIFIED', 'UNMAPPED', true,
     'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('ITM-DEMO-03', 'CEM-OPC-53', 'OPC 53 Grade Cement', 'BAG', 'Raw Material', '2523',
     'Ordinary Portland Cement, 53 grade, 50kg bag -- mirrored from the mock '
     'Zoho ERP adapter fixture, never a live connection.',
     'ZOHO', 'ZOHO_ERP', 'ZOHO-ITM-1001', '2026-08-01T09:00:00+00:00',
     'sha256:demo-fixture-only-not-a-real-payload-hash',
     'MOCK', 'UNMAPPED', false, 'SEED-SCRIPT', 'SEED-SCRIPT');

-- The duplicate flag itself, set the same way app.backend.pg.masters
-- would set it on create/ingest: the SECOND row entered points at the
-- first, never the reverse, and never merged.
UPDATE item_master SET duplicate_of = 'ITM-DEMO-01', mapping_status = 'DUPLICATE_SUSPECT'
WHERE item_id = 'ITM-DEMO-02';

-- --------------------------------------------------------------- vendors
INSERT INTO vendor_master
    (vendor_id, code, name, gst_no, gst_treatment, place_of_contact, pan_no, description,
     source, external_source, external_id, external_last_modified, payload_sha,
     source_of_truth_status, mapping_status, is_active, created_by, updated_by)
VALUES
    ('VEN-DEMO-01', 'VND-STEEL-01', 'Bharat Steel Traders',
     '21ABCDE1234A1Z5', 'registered_business', 'Odisha', 'ABCDE1234F',
     'Primary steel supplier for the demo estate -- carries both gst_no and '
     'pan_no so tax-identity masking (default) and reveal (permissioned, '
     'audited) are both demonstrable end to end.',
     'LOCAL', NULL, NULL, NULL, NULL, 'LOCAL', 'UNMAPPED', true,
     'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('VEN-DEMO-02', 'VND-STEEL-01-ALT', 'Bharat Steel Traders',
     NULL, NULL, NULL, NULL,
     'Imported vendor list entry naming the same supplier as VND-STEEL-01 '
     'under a different code -- the duplicate-name case, distinct from the '
     'duplicate-code case ITM-DEMO-02 demonstrates.',
     'IMPORT', NULL, NULL, NULL, NULL, 'UNVERIFIED', 'UNMAPPED', true,
     'SEED-SCRIPT', 'SEED-SCRIPT'),
    ('VEN-DEMO-03', 'VND-CEMENT-01', 'Konark Cement Distributors',
     NULL, NULL, NULL, NULL,
     'Mirrored from the mock Zoho ERP vendor adapter fixture.',
     'ZOHO', 'ZOHO_ERP', 'ZOHO-VEN-2001', '2026-08-01T09:05:00+00:00',
     'sha256:demo-fixture-only-not-a-real-payload-hash',
     'MOCK', 'UNMAPPED', true, 'SEED-SCRIPT', 'SEED-SCRIPT');

UPDATE vendor_master SET duplicate_of = 'VEN-DEMO-01', mapping_status = 'DUPLICATE_SUSPECT'
WHERE vendor_id = 'VEN-DEMO-02';

-- ----------------------------------------------------------- custom fields
-- REQ-SEC-007: one worked example, applicable to both items and vendors, so
-- the three-table shape (def / applicability / value) is exercised by the
-- seed rather than only by tests.
INSERT INTO custom_field_def
    (field_def_id, code, label, data_type, select_options, is_required, is_active,
     created_by, updated_by)
VALUES
    ('CF-DEMO-PREF-VENDOR', 'preferred_vendor_flag', 'Preferred Vendor', 'BOOLEAN',
     NULL, false, true, 'SEED-SCRIPT', 'SEED-SCRIPT');

INSERT INTO custom_field_applicability
    (applicability_id, field_def_id, applies_to, is_active, created_by)
VALUES
    ('CFA-DEMO-VENDOR', 'CF-DEMO-PREF-VENDOR', 'VENDOR', true, 'SEED-SCRIPT');

INSERT INTO custom_field_value
    (value_id, field_def_id, object_type, object_id, value, created_by, updated_by)
VALUES
    ('CFV-DEMO-01', 'CF-DEMO-PREF-VENDOR', 'VENDOR', 'VEN-DEMO-01', 'true',
     'SEED-SCRIPT', 'SEED-SCRIPT');
