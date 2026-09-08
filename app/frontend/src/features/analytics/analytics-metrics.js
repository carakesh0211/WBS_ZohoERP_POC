/* app/frontend/src/features/analytics/analytics-metrics.js
   Money, and the one arithmetic this feature is allowed to do.

   NO FLOAT TOUCHES MONEY. EVER.
   -----------------------------
   `0.1 + 0.2 !== 0.3` is not a curiosity here, it is the reason CAPEX totals
   drift by a paisa and then by a rupee and then a controller signs off a
   number that does not tie. Every money value in this feature is an INTEGER
   NUMBER OF PAISE, all the way from PostgreSQL's `bigint` to the string on
   screen, and every operation on it in this file is integer-only:

     * summing uses BigInt, so it cannot lose precision even above 2^53 and
       cannot round;
     * ratios are computed as integer BASIS POINTS (parts per 10 000) by
       integer division, never as a floating quotient;
     * the percentage a band bar is drawn at is turned into a CSS width string
       by integer digit assembly — `${bp / 100}%` would be a float division and
       is exactly what is NOT done below;
     * formatting is delegated to `core/format.js::formatINR`, which was
       already written this way (floor and modulo on integers, Indian digit
       grouping, negatives in parentheses so colour never carries the sign).

   `Number` is still the transport type, because that is what `JSON.parse`
   produces and converting the whole payload would be worse. What is enforced
   instead is that every value entering an arithmetic path is checked to be a
   SAFE INTEGER, and a value that is not is refused loudly rather than
   silently coerced — a `12345.67` arriving where paise were promised means a
   server sent rupees, and rendering it as `₹123.46` would hide a defect that
   is off by a factor of a hundred.

   THE SUMS-BACK CHECK
   -------------------
   The Wave 7 contract: "the card's figure must EQUAL the sum of the rows its
   drill-down returns. Assert it." `assertSumsBack()` is that assertion. It is
   deliberately NOT a repair: when the two disagree it returns the discrepancy
   in paise and the screen renders a DATA-QUALITY state naming it. Quietly
   replacing the card with the sum of the rows would hide exactly the defect
   the check exists to find — a filter applied to one and not the other, a
   paginated drill-down, a join that double counts.
*/

import { formatINR } from '../../core/format.js';

/* ------------------------------------------------------------------ *
 * Integer discipline
 * ------------------------------------------------------------------ */

/**
 * A value that is definitely an integer number of paise, or `null`.
 *
 * `null` and `undefined` pass through as `null` — an absent figure is a real
 * answer and is rendered as "not reported", never as zero. Zero and "we did
 * not receive a number" are different claims and collapsing them is how a
 * screen comes to report a clean ₹0.00 for a metric it never read.
 *
 * @throws {TypeError} on a non-integer or unsafe numeric value.
 */
export function paise(value, what = 'a money value') {
  if (value === null || value === undefined) return null;
  if (typeof value === 'bigint') return Number(value);
  const n = typeof value === 'string' && /^-?\d+$/.test(value.trim())
    ? Number(value.trim())
    : value;
  if (typeof n !== 'number' || !Number.isFinite(n)) {
    throw new TypeError(`${what}: expected integer paise, received ${JSON.stringify(value)}.`);
  }
  if (!Number.isInteger(n)) {
    throw new TypeError(
      `${what}: ${n} is not an integer. Money is integer paise end to end; a fractional value `
      + 'here means a server sent rupees, and rendering it would be wrong by a factor of 100.',
    );
  }
  if (!Number.isSafeInteger(n)) {
    throw new TypeError(`${what}: ${n} exceeds the safe integer range and cannot be trusted.`);
  }
  return n;
}

/**
 * Exact sum of integer paise, accumulated as BigInt.
 *
 * BigInt rather than `+` is not defensive decoration: a portfolio total is a
 * sum of sums, and `Number` addition is exact only while every intermediate
 * stays under 2^53. Accumulating in BigInt removes the question entirely, and
 * the single conversion back at the end is checked.
 *
 * `null` entries are SKIPPED and counted, so a caller can tell "these summed
 * to X" from "these summed to X and three rows reported nothing".
 *
 * @returns {{total: number, counted: number, missing: number}}
 */
export function sumPaise(values, what = 'a money column') {
  let acc = 0n;
  let counted = 0;
  let missing = 0;
  for (const value of values || []) {
    const p = paise(value, what);
    if (p === null) { missing += 1; continue; }
    acc += BigInt(p);
    counted += 1;
  }
  const total = Number(acc);
  if (!Number.isSafeInteger(total)) {
    throw new RangeError(`${what}: the total ${acc.toString()} paise exceeds the safe integer range.`);
  }
  return { total, counted, missing };
}

/** Sum one key across rows. */
export function sumColumn(rows, key) {
  return sumPaise((rows || []).map((r) => (r ? r[key] : null)), `column "${key}"`);
}

/**
 * A ratio in integer BASIS POINTS (parts per 10 000).
 *
 * `Math.round(a / b * 100)` is a float division followed by a rounding, and it
 * is how 99.995% becomes 100% on a screen whose whole job is to say whether a
 * budget was breached. This does the division once, in integers, and truncates
 * — so a bar that has not reached the line never draws as though it had.
 *
 * @returns {number|null} null when the denominator is zero or unknown; a
 *   utilisation of "nothing over nothing" is not 0% and is not 100%.
 */
export function basisPoints(numerator, denominator) {
  const n = paise(numerator, 'ratio numerator');
  const d = paise(denominator, 'ratio denominator');
  if (n === null || d === null || d === 0) return null;
  const scaled = (BigInt(n) * 10000n) / BigInt(d);
  return Number(scaled);
}

/**
 * Basis points to a display percentage, assembled from integer digits.
 *
 * `(bp / 100).toFixed(2)` is a float division. This is not.
 */
export function formatBp(bp) {
  if (bp === null || bp === undefined) return '—';
  const neg = bp < 0;
  const abs = Math.abs(Math.trunc(bp));
  const whole = Math.floor(abs / 100);
  const frac = abs % 100;
  return `${neg ? '−' : ''}${whole}.${String(frac).padStart(2, '0')}%`;
}

/**
 * A CSS width for a band bar, clamped to 0…100, assembled from integers.
 *
 * Applied through `setGeometry()` (the CSSOM), never as a `style="…"`
 * attribute — `style-src 'self'` blocks the attribute outright and
 * `core/dom.js::h()` throws on one.
 */
export function bpWidth(bp) {
  if (bp === null || bp === undefined) return '0%';
  const clamped = Math.min(10000, Math.max(0, Math.trunc(bp)));
  const whole = Math.floor(clamped / 100);
  const frac = clamped % 100;
  return `${whole}.${String(frac).padStart(2, '0')}%`;
}

/** Integer paise to the approved INR string. Re-exported so screens import once. */
export const inr = formatINR;

/**
 * The utilisation band, from integer basis points.
 *
 * The thresholds match the shell's existing bands so a figure does not change
 * colour by moving between screens. `null` in, `unknown` out: a project with
 * no budget has no utilisation and is not "safe".
 */
export function bandOf(bp) {
  if (bp === null || bp === undefined) return 'unknown';
  if (bp > 10000) return 'breach';
  if (bp >= 9000) return 'watch';
  return 'safe';
}

/* ------------------------------------------------------------------ *
 * The drill-down round trip
 * ------------------------------------------------------------------ */

/**
 * Does the card's figure equal the sum of the rows its drill-down returned?
 *
 * @param {number|null} cardPaise - the figure the card rendered, as the
 *   SERVER sent it. Never recomputed before being checked, or the check would
 *   be comparing a sum with itself.
 * @param {Array<Object>} rows - the drill-down's rows.
 * @param {string} key - the money column on those rows.
 * @returns {{ok: boolean, checked: boolean, card: number|null, rows: number,
 *             sum: number|null, difference: number|null, missing: number,
 *             message: string}}
 *
 * `checked: false` means the comparison was not possible — the card reported
 * no figure, or every row reported none — and that is reported as its own
 * outcome. A check that could not run must never come back as a pass; "we did
 * not look" and "it balances" are the two sentences this whole feature exists
 * to keep apart.
 */
export function assertSumsBack(cardPaise, rows, key) {
  const card = paise(cardPaise, 'the card figure');
  if (card === null) {
    return {
      ok: false,
      checked: false,
      card: null,
      rows: (rows || []).length,
      sum: null,
      difference: null,
      missing: 0,
      message: 'The card reported no figure, so there was nothing to check the drill-down '
        + 'against. This is not a pass.',
    };
  }
  const { total, counted, missing } = sumColumn(rows, key);
  if (counted === 0) {
    return {
      ok: false,
      checked: false,
      card,
      rows: (rows || []).length,
      sum: null,
      difference: null,
      missing,
      message: `The drill-down returned ${(rows || []).length} row(s) and not one of them `
        + `carried a "${key}" value, so the card's figure could not be checked against them.`,
    };
  }
  const difference = card - total;
  return {
    ok: difference === 0,
    checked: true,
    card,
    rows: (rows || []).length,
    sum: total,
    difference,
    missing,
    message: difference === 0
      ? `The card's ${inr(card)} is exactly the sum of the ${counted} row(s) behind it.`
      : `The card shows ${inr(card)} and the ${counted} row(s) behind it sum to ${inr(total)} — `
        + `a difference of ${inr(Math.abs(difference))}. ${difference > 0
          ? 'The drill-down is missing value the card counted: it is either filtered differently '
            + 'or paginated, and the rows on screen are not the whole basis of the figure above.'
          : 'The drill-down carries more value than the card: something is being counted twice, '
            + 'most often a join that fans out a line across its receipts.'} `
        + 'Neither figure is safe to sign off until this is resolved.',
  };
}

/**
 * The same check across several metrics at once, for a card band.
 *
 * @param {Array<{label:string, card:number|null, key:string}>} metrics
 * @param {Array<Object>} rows
 * @returns {Array<Object>} one result per metric, each carrying its label.
 */
export function assertBandSumsBack(metrics, rows) {
  return (metrics || []).map((m) => ({
    label: m.label,
    ...assertSumsBack(m.card, rows, m.key),
  }));
}
