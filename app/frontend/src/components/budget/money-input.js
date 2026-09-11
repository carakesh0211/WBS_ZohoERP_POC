/* app/frontend/src/components/budget/money-input.js
   Rupees-in / paise-out conversion for a plain text money field.

   Money is integer paise everywhere per docs/WAVE2_CONTRACTS.md ("Money —
   Integer paise everywhere ... No float, no Decimal, no rupee value, in
   code, fixtures or seeds") and the build brief's absolute rule 5. This
   module never routes the amount through a JS float: the rupees/paise split
   is done on the string itself, and the whole-rupees part is combined with
   the two-decimal-digit fraction using BigInt, exactly the way
   app.js::rupees() avoids floats on the legacy screens.

   BigInt is also how this module honours the note on
   core/format.js::formatINR — that helper is documented to be exact only
   below ~2^53 paise because it routes the value through Number. Rather than
   silently truncate a larger amount, parseRupeesToPaise() reports it via a
   thrown, user-facing error instead of ever handing an unsafe number to the
   API payload.

   Fable 5.1 (migration 029): a purchase order can be raised in the vendor's
   own currency, and not every currency has two decimals -- a yen amount
   parsed as if it had two is booked a HUNDRED times too high, a dinar ten
   times too low (app/backend/money.py::MINOR_EXPONENTS). parseAmountToMinor()
   is the same string-only parse at an explicit exponent (0..4, the range
   fx.translate_to_base_paise accepts); parseRupeesToPaise() is exactly that
   at exponent 2, so the existing callers see no change in behaviour or in
   wording.
*/

const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);

/**
 * Parse a user-entered amount in a currency's major units into an integer
 * number of that currency's minor units, at an explicit exponent.
 *
 * @param {string} raw
 * @param {number} exponent - the currency's ISO 4217 minor-unit exponent
 *   (INR/USD 2, JPY 0, KWD 3); 0..4.
 * @param {Object} [opts]
 * @param {string} [opts.unit='rupee'] - the word for the major unit in the
 *   error wording, e.g. 'JPY'.
 * @param {string} [opts.example] - an example figure for the error wording.
 * @returns {number} integer minor units, within Number.MAX_SAFE_INTEGER
 * @throws {Error} with a message safe to show directly under the field.
 */
export function parseAmountToMinor(raw, exponent, { unit = 'rupee', example } = {}) {
  const exp = Number(exponent);
  if (!Number.isInteger(exp) || exp < 0 || exp > 4) {
    throw new Error(`The currency's minor-unit exponent must be 0..4; got ${String(exponent)}.`);
  }
  const s = String(raw ?? '').trim().replace(/[,\s₹]/g, '');
  if (!s) throw new Error('Amount is required.');
  const pattern = exp === 0 ? /^\d+$/ : new RegExp(`^\\d+(\\.\\d{1,${exp}})?$`);
  if (!pattern.test(s)) {
    const sample = example || (exp === 2 ? '25,00,000.50' : (exp === 0 ? '250000' : `2500.${'5'.padEnd(exp, '0')}`));
    throw new Error(exp === 0
      ? `Amount must be a non-negative whole number of ${unit} (this currency has no minor unit), for example ${sample}.`
      : `Amount must be a non-negative ${unit} value with at most ${exp === 1 ? 'one decimal place' : `${['', '', 'two', 'three', 'four'][exp]} decimal places`}, for example ${sample}.`);
  }
  const [whole, fracRaw = ''] = s.split('.');
  const frac = (fracRaw + '0'.repeat(exp)).slice(0, exp);
  const minor = BigInt(whole) * (10n ** BigInt(exp)) + (exp ? BigInt(frac) : 0n);
  if (minor > MAX_SAFE) {
    throw new Error(
      'This amount is too large to represent exactly as an integer number of minor units in this browser session. '
      + 'Report this to engineering rather than submitting it — see core/format.js::formatINR.',
    );
  }
  return Number(minor);
}

/**
 * Parse a user-entered rupees string into an integer paise value.
 * @param {string} raw
 * @returns {number} integer paise, guaranteed within Number.MAX_SAFE_INTEGER
 * @throws {Error} with a message safe to show directly under the field, if
 *   the input is not a valid non-negative rupee amount, or if it is so large
 *   that representing it as a JS Number paise value would not be exact.
 */
export function parseRupeesToPaise(raw) {
  return parseAmountToMinor(raw, 2);
}
