/* app/frontend/src/components/settings/capex-source-badge.js
   Source badge for item/vendor master rows — LOCAL / IMPORT / ZOHO — plus,
   for ZOHO rows, the external id and last-synchronised time and an honest
   "not verified" indicator.

   ui-contract.md: "Nothing renders as LIVE or VERIFIED. A Zoho-sourced row
   is MOCK/unverified until proven otherwise against an authorised sandbox,
   and the badge must say so. The existing mockBadge() convention in app.js
   is the precedent — do not delete honesty indicators to make a screen look
   finished." app.js's .mock-chip class lives in the byte-frozen styles.css
   this screen is not allowed to edit, so this module reuses that exact
   class name rather than declaring a new one — the same reuse pattern
   capex-statuschip.js already applies to .status/.st-*.
*/

import { h, text } from '../../core/dom.js';
import { statusChip } from '../capex-statuschip.js';
import { formatSettingsTimestamp } from '../../features/settings/format.js';

const SOURCE_TONE = { LOCAL: 'neutral', IMPORT: 'progress', ZOHO: 'progress' };

/**
 * @param {Object} row - a masters row per docs/WAVE2_CONTRACTS.md
 *   (source, external_source, external_id, external_last_modified).
 */
export function sourceBadge(row) {
  const source = row && row.source;
  const tone = SOURCE_TONE[source] || 'neutral';
  const parts = [statusChip({ label: source || 'UNKNOWN', tone })];

  if (source === 'ZOHO') {
    parts.push(h('span', {
      class: 'mock-chip',
      title: 'No authorised Zoho sandbox has verified this record end-to-end. Treat every Zoho-sourced '
        + 'field here as unconfirmed until an authorised connection proves it.',
    }, 'MOCK · NOT VERIFIED'));
  }
  return h('span', { class: 'source-badge-group' }, parts);
}

/** The external-id / last-synchronised detail line shown for external rows. */
export function externalDetail(row) {
  if (!row || row.source === 'LOCAL' || !row.external_id) return null;
  return h('div', { class: 'xs muted external-detail' }, [
    text(`${row.external_source || 'External'} ID ${row.external_id}`),
    row.external_last_modified
      ? text(` · last synchronised ${formatSettingsTimestamp(row.external_last_modified)}`)
      : null,
  ].filter(Boolean));
}
