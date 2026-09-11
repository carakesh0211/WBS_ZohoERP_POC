/* app/frontend/src/features/procurement/fx-translate.js
   The INR figure a foreign-currency purchase order will be committed at,
   computed on the screen the way the server computes it, so the preview
   the user reads is the number the ledger will hold.

   This mirrors app/backend/pg/fx.py::translate_document exactly:

     1. the source line amounts (integer minor units: cents, yen, fils) are
        SUMMED, and the sum is translated ONCE --
          paise = round_half_away_from_zero(total_minor x rate x 10^(2 - exponent))
        (fx.translate_to_base_paise, ROUND_HALF_UP on the exact product);
     2. the header paise are allocated back across the lines by
        money.split_pro_rata -- integer floor shares, then the leftover paise
        handed to the largest remainders, ties to the larger weight, then to
        the earlier line -- so the lines sum to the header EXACTLY.

   Nothing here is a float. The rate arrives as the exact decimal STRING the
   rate book answers ("83.25000000") and is split into an integer and a
   scale; every product is a BigInt; the one division is an integer
   division with the remainder inspected, which is what "half away from
   zero" is on non-negative money. Amounts are non-negative here (an order
   is for a positive sum or for nothing -- procurement_services refuses a
   negative line), so the sign symmetry the server's docstring discusses does
   not arise.

   THE SERVER IS STILL THE AUTHORITY. The screen shows this figure so the
   user can see what the rate makes of the vendor's quote before committing
   it; the amount actually written is the one the server derives, and the
   response document (amount_paise per line) is what the screen shows after
   the write. If the two ever disagreed the response would win, visibly.
*/

const TEN = 10n;

/**
 * Split an exact decimal string into (integer, scale).
 * "83.25" -> [8325n, 2]; "0.561" -> [561n, 3]; "7" -> [7n, 0].
 * @throws {Error} when the string is not a plain positive decimal.
 */
export function decimalParts(rate) {
  const s = String(rate ?? '').trim();
  if (!/^\d+(\.\d+)?$/.test(s)) throw new Error(`Not an exact decimal rate: ${s || '(empty)'}.`);
  const [whole, frac = ''] = s.split('.');
  const digits = BigInt(whole + frac);
  if (digits <= 0n) throw new Error('The rate must be greater than zero.');
  return [digits, frac.length];
}

/**
 * fx.translate_to_base_paise for a non-negative amount: minor units of the
 * source currency at `exponent` decimals -> INR paise, quantised once, half
 * away from zero.
 * @param {number|bigint} amountMinor
 * @param {string} rate - exact decimal string
 * @param {number} exponent - 0..4
 * @returns {bigint}
 */
export function translateMinorToPaise(amountMinor, rate, exponent) {
  const exp = Number(exponent);
  if (!Number.isInteger(exp) || exp < 0 || exp > 4) throw new Error(`Exponent must be 0..4; got ${String(exponent)}.`);
  const minor = BigInt(amountMinor);
  if (minor < 0n) throw new Error('A purchase order line is not negative.');
  const [rateDigits, scale] = decimalParts(rate);
  // paise = minor * rate * 10^(2 - exp) with rate = rateDigits / 10^scale.
  let numerator = minor * rateDigits;
  let denominator = TEN ** BigInt(scale);
  if (2 - exp >= 0) numerator *= TEN ** BigInt(2 - exp);
  else denominator *= TEN ** BigInt(exp - 2);
  const quotient = numerator / denominator;
  const remainder = numerator % denominator;
  // Half away from zero on a non-negative value: round up when 2r >= d.
  return remainder * 2n >= denominator ? quotient + 1n : quotient;
}

/**
 * money.split_pro_rata: allocate `total` across integer `weights` with no
 * paise lost -- floor shares, leftover to the largest remainders, ties to the
 * larger weight, then to the earlier line.
 * @param {bigint} total
 * @param {bigint[]} weights - non-negative, not all zero
 * @returns {bigint[]}
 */
export function splitProRata(total, weights) {
  const totalW = weights.reduce((a, w) => a + w, 0n);
  if (totalW <= 0n) throw new Error('Cannot allocate across zero total weight.');
  const raw = weights.map((w) => (total * w) / totalW);
  let remainder = total - raw.reduce((a, r) => a + r, 0n);
  const order = weights.map((w, i) => ({ i, rem: (total * w) % totalW, w }))
    .sort((a, b) => {
      if (a.rem !== b.rem) return a.rem > b.rem ? -1 : 1;
      if (a.w !== b.w) return a.w > b.w ? -1 : 1;
      return a.i - b.i;
    });
  for (let k = 0; remainder > 0n; k += 1, remainder -= 1n) raw[order[k % order.length].i] += 1n;
  return raw;
}

/**
 * fx.translate_document: `{ headerPaise, linePaise }` for one document's
 * source line minors at an exact decimal rate.
 * @param {Array<number|bigint>} lineMinors
 * @param {string} rate
 * @param {number} exponent
 * @returns {{ headerPaise: bigint, linePaise: bigint[] }}
 */
export function translateDocument(lineMinors, rate, exponent) {
  const minors = lineMinors.map((m) => BigInt(m));
  if (!minors.length) throw new Error('Cannot translate a document with no lines.');
  const total = minors.reduce((a, m) => a + m, 0n);
  const headerPaise = translateMinorToPaise(total, rate, exponent);
  if (total === 0n) return { headerPaise, linePaise: minors.map(() => 0n) };
  return { headerPaise, linePaise: splitProRata(headerPaise, minors) };
}

/**
 * Integer minor units -> "1,23,456.78"-style text at the currency's exponent,
 * Indian grouping on the major units, by string slicing. No division.
 * @param {number|bigint|string} minor
 * @param {number} exponent
 * @returns {string}
 */
export function formatMinor(minor, exponent) {
  if (minor === null || minor === undefined || minor === '') return '—';
  const exp = Number(exponent);
  const negative = String(minor).startsWith('-');
  const digits = String(minor).replace(/^-/, '').replace(/\D/g, '').padStart(exp + 1, '0');
  const whole = exp ? digits.slice(0, -exp) : digits;
  const fraction = exp ? digits.slice(-exp) : '';
  const last3 = whole.slice(-3);
  const rest = whole.slice(0, -3);
  const grouped = rest ? `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',')},${last3}` : last3;
  return `${negative ? '-' : ''}${grouped}${fraction ? `.${fraction}` : ''}`;
}

/** Integer paise -> "₹1,23,456.78". */
export function formatPaise(paise) {
  if (paise === null || paise === undefined || paise === '') return '—';
  return `₹${formatMinor(paise, 2)}`;
}
