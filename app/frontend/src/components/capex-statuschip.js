/* app/frontend/src/components/capex-statuschip.js
   Status chip — reuses the approved .status / .st-* pattern from styles.css
   verbatim (no new CSS): a coloured dot from ::before PLUS a text symbol PLUS
   a text label, so status is never carried by colour alone
   (research/30_contracts/C3_statuses.json semantic roles;
   ui-contract.md "status distinguishable without colour").
*/

import { h, text } from '../core/dom.js';

const SYM = { positive: '✔', negative: '✖', warning: '!', progress: '◐', neutral: '·' };

/**
 * @param {Object} opts
 * @param {string} opts.label - visible text. Never omit this: a status
 *   rendered with no text label is exactly the failure mode this component
 *   exists to prevent.
 * @param {'positive'|'negative'|'warning'|'progress'|'neutral'} [opts.tone]
 * @param {string} [opts.title] - optional tooltip / accessible detail.
 */
export function statusChip({ label, tone = 'neutral', title } = {}) {
  if (!label) throw new Error('statusChip(): label is required — a status chip must never render text-less.');
  const role = SYM[tone] ? tone : 'neutral';
  const attrs = { class: `status st-${role}` };
  if (title) attrs.title = title;
  return h('span', attrs, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, SYM[role]),
    text(label),
  ]);
}

/** Chain-verification result rendered as the same non-colour status pattern. */
export function chainStatusChip(intact) {
  return intact
    ? statusChip({ label: 'Chain intact', tone: 'positive' })
    : statusChip({ label: 'Chain broken', tone: 'negative' });
}
