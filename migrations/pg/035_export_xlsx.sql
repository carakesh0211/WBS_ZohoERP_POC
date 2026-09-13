-- 035_export_xlsx.sql
-- An export job may be requested as a spreadsheet (.xlsx) as well as CSV.
--
-- WHAT THIS TRANSCRIBES (product owner, 2026-09-13, Stream C)
-- ===========================================================
--
-- "True .xlsx export for every report, register, dashboard view,
-- reconciliation and tabular screen ... async via the existing export-job
-- architecture, chunking, Summary / Data / Applied Filters / Metadata
-- sheets, frozen headers, autofilter, widths, date formats, Indian currency
-- format, numeric cells, subtotal formulas over trusted ranges, no external
-- or volatile formulas, formula-injection guard, filename with report name
-- and date."
--
-- HOW LITTLE THE SCHEMA CHANGES, AND WHY
-- ======================================
--
-- The job's chunks stay what 018 made them: append-only CSV text, digested at
-- completion and re-verified before a byte is served. The workbook is a
-- RENDERING of that verified result, built at download by
-- `pg/exports_xlsx.py` from the same chunks and the same metadata -- so the
-- integrity story (contiguous chunks, counted rows, SHA-256 of the text) is
-- unchanged and applies to both formats, and no binary blob is stored twice.
-- All this migration does is admit the second format in the CHECK, under the
-- SAME constraint name, so `tests/test_pg_exports.py`'s named-constraint
-- assertion keeps finding it.
--
-- Nothing here edits a byte of 001..034.

BEGIN;

ALTER TABLE export_job
    DROP CONSTRAINT ck_export_job_output_format;

ALTER TABLE export_job
    ADD CONSTRAINT ck_export_job_output_format
        CHECK (output_format IN ('csv', 'xlsx'));

COMMENT ON COLUMN export_job.output_format IS
    'csv (018) or xlsx (035). The chunks are CSV text either way; an xlsx job renders the verified CSV result into a workbook at download (pg/exports_xlsx.py).';

-- Migration 035 is applied by app/backend/pg/migrate_pg.py, which records it in
-- schema_migrations as part of the same transaction.
COMMIT;

-- ROLLBACK:
--
--   BEGIN;
--   -- An xlsx job cannot satisfy the narrower CHECK; cancel or purge them
--   -- (they are TTL-bound and their chunks are CSV) before re-adding it.
--   UPDATE export_job SET output_format = 'csv' WHERE output_format = 'xlsx';
--   ALTER TABLE export_job DROP CONSTRAINT ck_export_job_output_format;
--   ALTER TABLE export_job
--       ADD CONSTRAINT ck_export_job_output_format CHECK (output_format IN ('csv'));
--   DELETE FROM schema_migrations WHERE version = '035';
--   COMMIT;
