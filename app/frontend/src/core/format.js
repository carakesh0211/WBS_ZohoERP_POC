/* app/frontend/src/core/format.js
   Formatting helpers for the ES-module screens: audit timestamps, hash
   truncation and integer-paise INR — matching the conventions already in
   app.js (Indian digit grouping, tabular numerals, parentheses for negative)
   and research/30_contracts/C6_tokens.json's number_and_date_format block.
*/

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/**
 * Audit timestamps are the one place C6_tokens.json requires an explicit
 * timezone label ("Display in the organisation's timezone with an explicit
 * label on audit timestamps"). The API returns ISO-8601; rendered in UTC with
 * an explicit "UTC" label rather than the browser's local zone, so the same
 * audit record reads identically for every reviewer regardless of where they
 * sit — correctness matters more than convenience for an audit trail.
 */
export function formatAuditTimestamp(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const dd = String(d.getUTCDate()).padStart(2, '0');
  const mmm = MONTHS[d.getUTCMonth()];
  const yyyy = d.getUTCFullYear();
  const hh = String(d.getUTCHours()).padStart(2, '0');
  const mi = String(d.getUTCMinutes()).padStart(2, '0');
  const ss = String(d.getUTCSeconds()).padStart(2, '0');
  return `${dd}-${mmm}-${yyyy} ${hh}:${mi}:${ss} UTC`;
}

/**
 * Truncate a hex hash for table display while keeping enough of both ends to
 * spot-compare. The full value always remains available (callers should pass
 * it as a `title` attribute) — this is a display convenience, never a
 * substitute for the real value used in verification.
 */
export function truncateHash(hash, keep = 8) {
  if (!hash) return '—';
  const s = String(hash);
  if (s.length <= keep * 2 + 1) return s;
  return `${s.slice(0, keep)}…${s.slice(-6)}`;
}

/**
 * Integer paise -> INR string, Indian digit grouping, 2 decimal places,
 * negative values in parentheses (colour is never the sole carrier of sign —
 * C6_tokens.json financial.rule).
 */
export function formatINR(paise) {
  if (paise === null || paise === undefined) return '—';
  const n = Number(paise);
  if (!Number.isFinite(n)) return '—';
  const neg = n < 0;
  const abs = Math.abs(Math.round(n));
  const whole = Math.floor(abs / 100);
  const frac = abs % 100;
  let s = String(whole);
  if (s.length > 3) {
    const last3 = s.slice(-3);
    let rest = s.slice(0, -3);
    const parts = [];
    while (rest.length > 2) { parts.unshift(rest.slice(-2)); rest = rest.slice(0, -2); }
    if (rest) parts.unshift(rest);
    s = parts.join(',') + ',' + last3;
  }
  const out = '₹' + s + '.' + String(frac).padStart(2, '0');
  return neg ? `(${out})` : out;
}
