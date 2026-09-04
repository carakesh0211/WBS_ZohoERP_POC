/* app/frontend/src/components/approvals/approval-status-badge.js
   The three approval-engine status namespaces, rendered so that status is
   legible WITHOUT colour.

   research/30_contracts/C15_approval_statuses.json is the authority. It
   declares three NAMESPACE-DISJOINT vocabularies — instance, stage and
   assignment — and states that these codes are INTERNAL: they may be surfaced
   on approval screens (SCR-03, SCR-29) and nowhere else, and an approval
   status must never be rendered on a business screen as if it were a C3
   business status.

   Four codes — APPROVED, REJECTED, RETURNED, EXCEPTION_PENDING — share a
   LABEL with a C3 business status. That overlap is reviewed and deliberate,
   and it is exactly why this module exists as a sibling of
   components/capex-statuschip.js rather than as a call into it: an approval
   instance being APPROVED is a decision on one object version, whereas a
   purchase order being APPROVED is a document lifecycle state. Same word,
   different fact. Keeping the two renderers apart keeps the two identifiers
   apart.

   Non-colour indicator (C3 `non_colour_indicator_required`, C6
   `status_icons_required`): every badge carries a TEXT glyph plus a TEXT
   label, and the glyphs are pairwise distinct WITHIN a namespace. The glyph
   is aria-hidden because the label beside it already says the same thing to a
   screen reader, and the colour is a third, redundant channel — never the
   only one.
*/

import { h, text } from '../../core/dom.js';

/* Tone is one of the five semantic roles in C3_statuses.json, which map onto
   the frozen .st-* classes in styles.css. No new colour is introduced. */

const INSTANCE = {
  OPEN: { tone: 'progress', glyph: '◐', label: 'Open', meaning: 'At least one stage is pending. The object cannot proceed.' },
  APPROVED: { tone: 'positive', glyph: '✔', label: 'Approved', meaning: 'Every applying stage met quorum.' },
  REJECTED: { tone: 'negative', glyph: '✖', label: 'Rejected', meaning: 'Terminal for this object version; a new version may be raised.' },
  RETURNED: { tone: 'warning', glyph: '↩', label: 'Returned', meaning: 'Sent back to the requestor for correction.' },
  RECALLED: { tone: 'neutral', glyph: '⤺', label: 'Recalled', meaning: 'Withdrawn by the maker before a decision.' },
  SUPERSEDED: { tone: 'neutral', glyph: '⧉', label: 'Superseded', meaning: 'The object changed under an open instance and was re-routed. Never a silent approval.' },
  EXCEPTION_PENDING: { tone: 'warning', glyph: '⚠', label: 'Exception pending', meaning: 'Routing could not complete. This FAILS CLOSED and needs an administrator; it has never auto-approved and never will.' },
};

const STAGE = {
  PENDING: { tone: 'progress', glyph: '◐', label: 'Pending', meaning: 'Open and awaiting decisions.' },
  APPROVED: { tone: 'positive', glyph: '✔', label: 'Approved', meaning: 'Quorum met.' },
  REJECTED: { tone: 'negative', glyph: '✖', label: 'Rejected', meaning: 'Rejected at this stage.' },
  RETURNED: { tone: 'warning', glyph: '↩', label: 'Returned', meaning: 'Returned at this stage.' },
  SKIPPED: { tone: 'neutral', glyph: '⊘', label: 'Skipped', meaning: 'The stage did not apply. Recorded, never omitted — an auditor must see what did not run, and why.' },
  ESCALATED: { tone: 'warning', glyph: '⇧', label: 'Escalated', meaning: 'SLA breached; capacity extended to the escalation target. The original assignees remain pending.' },
};

const ASSIGNMENT = {
  PENDING: { tone: 'progress', glyph: '◐', label: 'Pending', meaning: 'Assigned, no action taken.' },
  ACTED: { tone: 'positive', glyph: '✔', label: 'Acted', meaning: 'A recorded decision exists for this assignee on this stage.' },
  WITHDRAWN: { tone: 'neutral', glyph: '·', label: 'Withdrawn', meaning: 'Assignment removed — role revoked, delegation expired, or the stage closed by quorum first.' },
};

const NAMESPACES = { instance: INSTANCE, stage: STAGE, assignment: ASSIGNMENT };

/**
 * Look up the display metadata for a status code within one namespace.
 *
 * An UNKNOWN code is never swallowed. It renders neutrally with a '?' glyph
 * and its own raw code as the label, so a status this build has not been
 * taught about is visible as an unknown rather than silently indistinguishable
 * from a known one.
 *
 * @param {'instance'|'stage'|'assignment'} namespace
 * @param {string} code
 * @returns {{tone: string, glyph: string, label: string, meaning: string}}
 */
export function approvalStatusMeta(namespace, code) {
  const table = NAMESPACES[namespace];
  const meta = table && code ? table[code] : null;
  if (meta) return meta;
  return {
    tone: 'neutral',
    glyph: '?',
    label: String(code || 'Unknown'),
    meaning: 'This status is not known to this build of the application.',
  };
}

/** Every code this module can render, for a namespace. Used by the tests. */
export function approvalStatusCodes(namespace) {
  return Object.keys(NAMESPACES[namespace] || {});
}

/**
 * A status badge.
 *
 * @param {'instance'|'stage'|'assignment'} namespace
 * @param {string} code
 * @param {Object} [opts]
 * @param {boolean} [opts.describe] - add the meaning as a title attribute.
 */
export function approvalStatusBadge(namespace, code, { describe = true } = {}) {
  const meta = approvalStatusMeta(namespace, code);
  const attrs = { class: `status st-${meta.tone} approval-status` };
  if (describe) attrs.title = meta.meaning;
  return h('span', attrs, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, meta.glyph),
    text(meta.label),
  ]);
}
