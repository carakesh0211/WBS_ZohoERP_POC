-- 010_fx_rates.sql
-- Fable 5.1 demo estate for migration 028: a small book of reference rates
-- for the four foreign currencies the demo bills arrive in, maintained the
-- way the Exchange Rates screen maintains them.
--
-- Loaded by app/backend/pg/seed.py in filename order, AFTER seed_demo.sql
-- (U-FIN and U-ADM) and under seed.py's profile and disposable-name guards,
-- which `--force` cannot bypass. Nothing here invents an identity or a
-- currency: every currency_code is one migration 023 seeded, and every actor
-- is a seed_demo.sql user.
--
-- WHAT THE DATA IS SHAPED TO SHOW
-- ===============================
--
-- * USD, EUR, JPY and KWD to INR, RBI reference quotes, created by U-FIN and
--   put into force by U-ADM -- two identities, because FX_ACTIVATION_SEPARATION
--   is REQUIRED and a seeded row that a single user both wrote and activated
--   would demonstrate the control being bypassed.
--
-- * A DATE GAP. Every currency is quoted for 3, 4, 5 and 7 August 2026 and
--   NOT for 6 August. A "Test lookup" of any pair on 2026-08-06 answers the
--   coded refusal FX_RATE_UNAVAILABLE, which is the behaviour the Bills
--   ingestion relies on (AUD-H-007): a bill dated the 6th is refused, not
--   translated at the 5th's rate, until somebody records the 6th's.
--
-- * ONE INACTIVE RATE. The EUR quote for 5 August was keyed as 98.10000000,
--   retired by U-ADM with a reason, and replaced by a correction at
--   97.91000000 that U-ADM then activated. The retired row keeps its value
--   and points at its replacement (superseded_by) so the history panel shows
--   the correction as a correction. A second, PENDING JPY row for the 7th
--   (created, never activated) shows what a rate awaiting its second pair of
--   eyes looks like, and that the lookup ignores it.
--
-- * JPY (exponent 0) and KWD (exponent 3), so the screen's translation
--   preview has a currency where "100" is a hundred yen and one where "1000"
--   is one dinar.
--
-- The rates are plausible round figures for demonstration, not quotations.

INSERT INTO fx_rate
    (fx_rate_id, from_currency, to_currency, rate_date, rate, rate_source,
     source_reference, captured_at, created_by, active, activated_at,
     activated_by, updated_by)
VALUES
    -- USD
    ('FXR-DM-USD-0803', 'USD', 'INR', DATE '2026-08-03', 83.72000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-03', TIMESTAMPTZ '2026-08-03 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-03 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-USD-0804', 'USD', 'INR', DATE '2026-08-04', 83.81000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-04', TIMESTAMPTZ '2026-08-04 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-04 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-USD-0805', 'USD', 'INR', DATE '2026-08-05', 83.77000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-05', TIMESTAMPTZ '2026-08-05 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-05 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-USD-0807', 'USD', 'INR', DATE '2026-08-07', 83.90000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-07', TIMESTAMPTZ '2026-08-07 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-07 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    -- EUR (the 5th is the corrected one; see below)
    ('FXR-DM-EUR-0803', 'EUR', 'INR', DATE '2026-08-03', 97.64000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-03', TIMESTAMPTZ '2026-08-03 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-03 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-EUR-0804', 'EUR', 'INR', DATE '2026-08-04', 97.80000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-04', TIMESTAMPTZ '2026-08-04 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-04 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-EUR-0805-A', 'EUR', 'INR', DATE '2026-08-05', 98.10000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-05 (keying error)', TIMESTAMPTZ '2026-08-05 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-05 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-EUR-0807', 'EUR', 'INR', DATE '2026-08-07', 97.95000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-07', TIMESTAMPTZ '2026-08-07 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-07 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    -- JPY, minor exponent 0
    ('FXR-DM-JPY-0803', 'JPY', 'INR', DATE '2026-08-03', 0.56100000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-03', TIMESTAMPTZ '2026-08-03 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-03 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-JPY-0804', 'JPY', 'INR', DATE '2026-08-04', 0.56300000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-04', TIMESTAMPTZ '2026-08-04 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-04 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-JPY-0805', 'JPY', 'INR', DATE '2026-08-05', 0.56200000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-05', TIMESTAMPTZ '2026-08-05 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-05 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    -- KWD, minor exponent 3
    ('FXR-DM-KWD-0803', 'KWD', 'INR', DATE '2026-08-03', 272.45000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-03', TIMESTAMPTZ '2026-08-03 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-03 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-KWD-0804', 'KWD', 'INR', DATE '2026-08-04', 272.80000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-04', TIMESTAMPTZ '2026-08-04 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-04 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-KWD-0805', 'KWD', 'INR', DATE '2026-08-05', 272.60000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-05', TIMESTAMPTZ '2026-08-05 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-05 14:10:00+05:30', 'U-ADM', 'U-ADM'),
    ('FXR-DM-KWD-0807', 'KWD', 'INR', DATE '2026-08-07', 273.05000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-07', TIMESTAMPTZ '2026-08-07 13:05:00+05:30', 'U-FIN',
     true, TIMESTAMPTZ '2026-08-07 14:10:00+05:30', 'U-ADM', 'U-ADM');

-- The EUR correction for the 5th: a NEW row, activated by U-ADM ...
INSERT INTO fx_rate
    (fx_rate_id, from_currency, to_currency, rate_date, rate, rate_source,
     source_reference, captured_at, created_by, active, activated_at,
     activated_by, note, updated_by)
VALUES
    ('FXR-DM-EUR-0805-B', 'EUR', 'INR', DATE '2026-08-05', 97.91000000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-05', TIMESTAMPTZ '2026-08-06 09:20:00+05:30', 'U-FIN',
     false, NULL, NULL,
     'Correction of FXR-DM-EUR-0805-A (98.10 was a keying error).', 'U-FIN');

-- ... which supersedes the keying error. Ordered so that the partial unique
-- index never sees two ACTIVE quotes for (EUR, INR, 2026-08-05, RBI_REFERENCE):
-- retire the old row first, then activate the correction.
UPDATE fx_rate
   SET active = false,
       deactivated_at = TIMESTAMPTZ '2026-08-06 09:25:00+05:30',
       deactivated_by = 'U-ADM',
       deactivation_reason = 'Superseded by FXR-DM-EUR-0805-B: 98.10 was a keying error.',
       superseded_by = 'FXR-DM-EUR-0805-B',
       updated_by = 'U-ADM'
 WHERE fx_rate_id = 'FXR-DM-EUR-0805-A';

UPDATE fx_rate
   SET active = true,
       activated_at = TIMESTAMPTZ '2026-08-06 09:25:00+05:30',
       activated_by = 'U-ADM',
       updated_by = 'U-ADM'
 WHERE fx_rate_id = 'FXR-DM-EUR-0805-B';

-- A PENDING quote: created by U-FIN, awaiting activation by somebody else.
-- The lookup for JPY on the 7th refuses until it is activated.
INSERT INTO fx_rate
    (fx_rate_id, from_currency, to_currency, rate_date, rate, rate_source,
     source_reference, captured_at, created_by, active, updated_by)
VALUES
    ('FXR-DM-JPY-0807', 'JPY', 'INR', DATE '2026-08-07', 0.56500000, 'RBI_REFERENCE',
     'RBI reference rate 2026-08-07', TIMESTAMPTZ '2026-08-07 13:05:00+05:30', 'U-FIN',
     false, 'U-FIN');
