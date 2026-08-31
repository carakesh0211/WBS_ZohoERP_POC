/* app/frontend/src/components/budget/verdict-badge.js
   SCR-13 Budget Availability Check — the four verdicts
   (OK | WATCH | CRITICAL | EXCEEDS_BUDGET) rendered so each is distinct
   without relying on colour: a distinct glyph AND a distinct label text per
   verdict, on top of the tone. Reuses the approved `.status st-*` pattern
   from styles.css verbatim (dot + colour text), the same pattern
   components/capex-statuschip.js uses — this is a sibling of that component
   rather than an edit to it, because capex-statuschip.js is shared/frozen
   and its fixed one-symbol-per-tone map cannot express two verdicts sharing
   a tone (CRITICAL and EXCEEDS_BUDGET both read as "negative") with two
   different glyphs.

   Tone mapping follows C6_tokens.json's own exposure-band language ("below
   80 percent safe, 80 to 90 percent watch, above 90 percent or exceeded
   breach"): OK is the safe band, WATCH and CRITICAL are two points on the
   watch/breach gradient, and EXCEEDS_BUDGET is the hard-stop case where the
   requested amount does not fit at all — a stronger, distinctly-iconed state
   layered on top of "breach" rather than folded into it.
*/

import { h, text } from '../../core/dom.js';

const VERDICTS = {
  OK: {
    tone: 'positive',
    glyph: '✔',
    label: 'OK',
    description: 'Within budget.',
  },
  WATCH: {
    tone: 'warning',
    glyph: '▲',
    label: 'Watch',
    description: 'Utilisation has crossed the watch threshold.',
  },
  CRITICAL: {
    tone: 'negative',
    glyph: '⚠',
    label: 'Critical',
    description: 'Utilisation has crossed the critical threshold. This request can still be funded.',
  },
  EXCEEDS_BUDGET: {
    tone: 'negative',
    glyph: '⛔',
    label: 'Exceeds budget',
    description: 'This request cannot be funded from the available budget.',
  },
};

/** @param {string} verdict one of OK|WATCH|CRITICAL|EXCEEDS_BUDGET */
export function verdictMeta(verdict) {
  return VERDICTS[verdict] || {
    tone: 'neutral', glyph: '?', label: verdict || 'Unknown', description: '',
  };
}

/**
 * @param {'OK'|'WATCH'|'CRITICAL'|'EXCEEDS_BUDGET'} verdict
 * @param {Object} [opts]
 * @param {boolean} [opts.large] - the SCR-13 result panel wants a larger,
 *   headline-style rendering; table cells (if ever needed) want the compact
 *   inline chip.
 */
export function verdictBadge(verdict, { large = false } = {}) {
  const meta = verdictMeta(verdict);
  const attrs = { class: `status st-${meta.tone} budget-verdict${large ? ' budget-verdict-lg' : ''}` };
  return h('span', attrs, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, meta.glyph),
    text(meta.label),
  ]);
}
