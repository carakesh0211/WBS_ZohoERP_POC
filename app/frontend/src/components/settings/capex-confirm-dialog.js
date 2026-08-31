/* app/frontend/src/components/settings/capex-confirm-dialog.js
   A small reusable confirmation dialog for the one irreversible-ish action
   this feature offers: deactivate (never delete — docs/WAVE2_CONTRACTS.md /
   the settings-frontend brief are explicit that master and organisation
   records are deactivated, never deleted). Per ui-contract.md, a dialog
   used for a destructive-leaning action must state the consequence
   explicitly, which is why `message` is required rather than optional.
*/

import { h, text, clear } from '../../core/dom.js';

export function createConfirmDialog() {
  const titleEl = h('h2', { id: 'settingsConfirmTitle' }, '');
  const closeBtn = h('button', {
    type: 'button', class: 'dlg-close', 'aria-label': 'Close dialog', onClick: () => dialogEl.close(),
  }, '✕');
  const bodyMsg = h('div', { class: 'dlg-body' });
  const errorHost = h('div', { class: 'dlg-msg' });
  const confirmBtn = h('button', {
    type: 'button',
    class: 'btn-danger btn-sm',
    onClick: () => runConfirm(),
  }, 'Deactivate');
  const cancelBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => dialogEl.close() }, 'Cancel');
  const foot = h('div', { class: 'dlg-foot' }, [cancelBtn, confirmBtn]);
  const dialogEl = h('dialog', { 'aria-labelledby': 'settingsConfirmTitle' }, [
    h('div', { class: 'dlg-head' }, [titleEl, closeBtn]), errorHost, bodyMsg, foot,
  ]);

  let session = null;
  let lastFocused = null;

  async function runConfirm() {
    if (!session) return;
    clear(errorHost);
    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Deactivating…';
    try {
      const result = await session.onConfirm();
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Deactivate';
      dialogEl.close();
      if (session.onSuccess) session.onSuccess(result);
    } catch (err) {
      confirmBtn.disabled = false;
      confirmBtn.textContent = 'Deactivate';
      errorHost.appendChild(h('div', { class: 'msg msg-error', role: 'alert' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
        h('div', { class: 'body' }, err && err.message ? err.message : 'This action could not be completed. Try again.'),
      ]));
    }
  }

  dialogEl.addEventListener('close', () => {
    session = null;
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  });

  function open({
    title, message, confirmLabel = 'Deactivate', onConfirm, onSuccess, opener,
  }) {
    session = { onConfirm, onSuccess };
    lastFocused = opener || document.activeElement;
    clear(errorHost);
    titleEl.textContent = title;
    clear(bodyMsg);
    bodyMsg.appendChild(typeof message === 'string' ? text(message) : message);
    confirmBtn.textContent = confirmLabel;
    if (!dialogEl.isConnected) document.body.appendChild(dialogEl);
    dialogEl.showModal();
    confirmBtn.focus();
  }

  return { el: dialogEl, open, close: () => dialogEl.close() };
}
