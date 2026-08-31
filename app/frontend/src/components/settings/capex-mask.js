/* app/frontend/src/components/settings/capex-mask.js
   Rendering for classified vendor-tax-identity fields (gst_no, pan_no).

   docs/WAVE2_CONTRACTS.md: "Vendor tax identity is masked by default...
   unless the caller holds the reveal permission — and a full reveal writes
   an audit entry." The frozen contract's GET /api/masters/vendors shape
   does not add a reveal endpoint or a permission flag on the row, so this
   module has nothing to call and nothing to unmask; it renders EXACTLY the
   string the server returned (masked or, if the server itself judged this
   caller privileged, not) and never reconstructs, strips or infers a value.
   The one thing it adds is honesty about what is not yet possible: a
   disabled control naming why a reveal cannot happen from this screen,
   rather than silently omitting the affordance the UI contract calls for.
   See the stream's final report for the exact contract gap this documents.
*/

import { h, text } from '../../core/dom.js';

const MASK_PATTERN = /\*{2,}/;

/**
 * @param {string|null|undefined} value - exactly what the API returned for
 *   gst_no or pan_no.
 * @param {Object} [opts]
 * @param {string} [opts.fieldLabel] - e.g. "GSTIN" / "PAN" for the tooltip.
 */
export function maskedIdentity(value, { fieldLabel = 'this identifier' } = {}) {
  if (value === null || value === undefined || value === '') {
    return h('span', { class: 'muted' }, '—');
  }
  const str = String(value);
  const isMasked = MASK_PATTERN.test(str);
  const chip = h('span', { class: 'mono' }, str);
  if (!isMasked) {
    // The server itself decided this caller may see the full value; the UI
    // states that plainly rather than implying it performed the reveal.
    return h('span', { class: 'mask-field' }, [
      chip,
      h('span', { class: 'mask-note xs muted' }, 'sent unmasked by the server'),
    ]);
  }
  return h('span', { class: 'mask-field' }, [
    chip,
    h('button', {
      type: 'button',
      class: 'btn-sm mask-reveal-btn',
      disabled: true,
      title: `Revealing ${fieldLabel} requires the reveal permission and is not yet exposed by the settings API — `
        + 'see the frontend stream report for this contract gap. The UI never reconstructs a masked value itself.',
    }, [text('Reveal'), h('span', { class: 'sr-only' }, ` ${fieldLabel} (unavailable)`)]),
  ]);
}
