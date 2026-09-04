/* app/frontend/src/components/approvals/reason-dialog.js
   A modal that asks for a reason before an approval-lifecycle action.

   WHY NOT window.prompt()

   Every action this dialog fronts — recall, cancel, resubmit, revoke a
   delegation — writes a `reason_text` into an append-only, hash-chained audit
   record. That reason is read later by someone reconstructing what happened,
   so capturing it properly matters. `window.prompt()` cannot be labelled, cannot
   be described to a screen reader beyond its bare string, cannot show a
   validation error, cannot show the SERVER's refusal, and is suppressed
   outright in some embedding contexts — in which case the action would either
   proceed with no reason or silently not happen at all.

   This follows the idiom already established in
   components/settings/capex-confirm-dialog.js: a native <dialog> with
   showModal(), which brings the browser's own focus trap and Escape handling
   rather than a hand-rolled one, plus focus restoration to the control that
   opened it. It reuses the frozen .dlg-* styling from styles.css and adds no
   CSS of its own.
*/

import { h, text, clear } from '../../core/dom.js';

let seq = 0;

/**
 * @param {Object} [config]
 * @param {boolean} [config.requireReason] - refuse to submit an empty reason.
 * @returns {{el: HTMLElement, open: Function, close: Function}}
 */
export function createReasonDialog({ requireReason = true } = {}) {
  seq += 1;
  const titleId = `approvalReasonTitle${seq}`;
  const fieldId = `approvalReasonText${seq}`;
  const errorId = `approvalReasonError${seq}`;

  const titleEl = h('h2', { id: titleId }, '');
  const closeBtn = h('button', {
    type: 'button', class: 'dlg-close', 'aria-label': 'Close dialog',
    onClick: () => dialogEl.close(),
  }, '✕');

  const describeEl = h('p', { class: 'muted small' });
  const reasonEl = h('textarea', { id: fieldId, rows: '4' });
  const bodyEl = h('div', { class: 'dlg-body' }, [
    describeEl,
    h('div', { class: 'field' }, [
      h('label', { for: fieldId }, 'Reason'),
      reasonEl,
    ]),
  ]);

  const errorHost = h('div', { class: 'dlg-msg', id: errorId });
  const confirmBtn = h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => run() }, 'Confirm');
  const cancelBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => dialogEl.close() }, 'Cancel');
  const dialogEl = h('dialog', { 'aria-labelledby': titleId }, [
    h('div', { class: 'dlg-head' }, [titleEl, closeBtn]),
    errorHost,
    bodyEl,
    h('div', { class: 'dlg-foot' }, [cancelBtn, confirmBtn]),
  ]);

  let session = null;
  let lastFocused = null;

  function showError(message) {
    clear(errorHost);
    errorHost.appendChild(h('div', { class: 'msg msg-error', role: 'alert' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
      h('div', { class: 'body' }, text(message)),
    ]));
  }

  async function run() {
    if (!session) return;
    const reason = reasonEl.value.trim();
    if (requireReason && !reason) {
      reasonEl.setAttribute('aria-invalid', 'true');
      reasonEl.setAttribute('aria-describedby', errorId);
      showError('Give a reason. It is recorded in the audit trail and read by whoever reviews this later.');
      reasonEl.focus();
      return;
    }
    reasonEl.removeAttribute('aria-invalid');
    clear(errorHost);

    const label = confirmBtn.textContent;
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Working…';
    try {
      const result = await session.onConfirm(reason);
      dialogEl.close();
      if (session.onSuccess) session.onSuccess(result);
    } catch (err) {
      // The refusal is shown INSIDE the dialog, with the reason still typed,
      // so the user can correct and retry rather than losing what they wrote.
      showError((err && err.message) || 'This action could not be completed. Try again.');
    } finally {
      confirmBtn.disabled = false;
      confirmBtn.textContent = label;
    }
  }

  dialogEl.addEventListener('close', () => {
    session = null;
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  });

  /**
   * @param {Object} opts
   * @param {string} opts.title
   * @param {string} [opts.describe] - what this action will do.
   * @param {string} [opts.confirmLabel]
   * @param {boolean} [opts.danger] - render the confirm control as destructive.
   * @param {(reason: string) => Promise<*>} opts.onConfirm
   * @param {Function} [opts.onSuccess]
   * @param {HTMLElement} [opts.opener] - focus returns here on close.
   */
  function open({
    title, describe = '', confirmLabel = 'Confirm', danger = false,
    onConfirm, onSuccess, opener,
  }) {
    session = { onConfirm, onSuccess };
    lastFocused = opener || document.activeElement;
    clear(errorHost);
    titleEl.textContent = title;
    describeEl.textContent = describe;
    describeEl.hidden = !describe;
    reasonEl.value = '';
    reasonEl.removeAttribute('aria-invalid');
    confirmBtn.textContent = confirmLabel;
    confirmBtn.className = danger ? 'btn-danger btn-sm' : 'btn-primary btn-sm';
    if (!dialogEl.isConnected) document.body.appendChild(dialogEl);
    dialogEl.showModal();
    reasonEl.focus();
  }

  return { el: dialogEl, open, close: () => dialogEl.close() };
}
