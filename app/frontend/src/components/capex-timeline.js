/* app/frontend/src/components/capex-timeline.js
   Audit entry timeline — actor, action and timestamp per entry, connected by
   a vertical rule. New layout rules live in extensions.css
   (.audit-timeline*), tokens only; the dot re-uses the same five semantic
   tones as capex-statuschip.js / .status so the two components read as one
   visual language.
*/

import { h, text } from '../core/dom.js';
import { formatAuditTimestamp, truncateHash } from '../core/format.js';

function actionTone(action) {
  const a = String(action || '').toLowerCase();
  if (a.includes('reject') || a.includes('fail') || a.includes('delete') || a.includes('cancel')) return 'negative';
  if (a.includes('warn') || a.includes('override') || a.includes('exception') || a.includes('return')) return 'warning';
  if (a.includes('approve') || a.includes('create') || a.includes('release') || a.includes('capitalis')) return 'positive';
  if (a.includes('submit') || a.includes('revis')) return 'progress';
  return 'neutral';
}

/**
 * @param {Object} opts
 * @param {Array<Object>} [opts.entries] - audit entries per the API contract.
 * @param {string} [opts.ariaLabel]
 * @param {string} [opts.emptyMessage]
 */
export function createTimeline({ entries = [], ariaLabel, emptyMessage } = {}) {
  if (!entries.length) {
    return h('div', { class: 'empty' }, [
      h('div', { class: 'big', 'aria-hidden': 'true' }, '⎙'),
      h('div', {}, emptyMessage || 'No audit entries to show on the timeline.'),
    ]);
  }

  const list = h('ol', { class: 'audit-timeline', ...(ariaLabel ? { 'aria-label': ariaLabel } : {}) });
  for (const entry of entries) {
    const objectLine = entry.object_type
      ? `${entry.object_type} ${entry.object_id ?? ''}`.trim()
      : null;
    list.appendChild(h('li', { class: 'audit-timeline-item' }, [
      h('span', { class: `audit-timeline-dot st-${actionTone(entry.action)}`, 'aria-hidden': 'true' }),
      h('div', { class: 'audit-timeline-body' }, [
        h('div', { class: 'audit-timeline-head' }, [
          h('span', { class: 'audit-timeline-actor' }, entry.actor || 'Unknown actor'),
          h('span', { class: 'audit-timeline-action' }, entry.action || '—'),
        ]),
        h('div', { class: 'audit-timeline-meta muted xs' }, [
          text(`Seq ${entry.seq} · ${formatAuditTimestamp(entry.at)}${objectLine ? ` · ${objectLine}` : ''}`),
        ]),
        entry.detail ? h('div', { class: 'audit-timeline-detail' }, entry.detail) : null,
        h('div', { class: 'audit-timeline-hash mono xs muted', title: entry.entry_hash || '' },
          `Hash ${truncateHash(entry.entry_hash)}`),
      ].filter(Boolean)),
    ]));
  }
  return list;
}
