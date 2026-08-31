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
*/

const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);

/**
 * Parse a user-entered rupees string into an integer paise value.
 * @param {string} raw
 * @returns {number} integer paise, guaranteed within Number.MAX_SAFE_INTEGER
 * @throws {Error} with a message safe to show directly under the field, if
 *   the input is not a valid non-negative rupee amount, or if it is so large
 *   that representing it as a JS Number paise value would not be exact.
 */
export function parseRupeesToPaise(raw) {
  const s = String(raw ?? '').trim().replace(/[,\s₹]/g, '');
  if (!s) throw new Error('Amount is required.');
  if (!/^\d+(\.\d{1,2})?$/.test(s)) {
    throw new Error('Amount must be a non-negative rupee value with at most two decimal places, for example 25,00,000.50.');
  }
  const [whole, fracRaw = ''] = s.split('.');
  const frac = (fracRaw + '00').slice(0, 2);
  const paise = BigInt(whole) * 100n + BigInt(frac);
  if (paise > MAX_SAFE) {
    throw new Error(
      'This amount is too large to represent exactly as integer paise in this browser session. '
      + 'Report this to engineering rather than submitting it — see core/format.js::formatINR.',
    );
  }
  return Number(paise);
}
