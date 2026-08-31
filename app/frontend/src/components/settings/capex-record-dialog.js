/* app/frontend/src/components/settings/capex-record-dialog.js
   A reusable create/edit dialog for a single settings/masters record.
   Reuses the frozen `dialog` / `.dlg-*` / `.field` / `.msg` markup pattern
   from styles.css verbatim (no new CSS needed for the chrome itself; only
   a handful of layout-only rules live in settings.css).

   Concurrency: docs/WAVE2_CONTRACTS.md — "body MUST carry version_no;
   mismatch -> 409 VERSION_CONFLICT". This dialog always resubmits the
   version_no it loaded the row with, and a 409 renders as an actionable,
   in-dialog conflict message with a "Reload latest values" action that
   re-fetches the row and repopulates the form — never a silent overwrite,
   and the dialog stays open so nothing already typed by the user vanishes
   without them choosing to reload.

   ZOHO read-only fields: docs/WAVE2_CONTRACTS.md — "PUT /api/masters/{kind}/
   {id} refuses to edit a ZOHO-sourced field" and the settings-frontend brief
   — "the user must be able to see *why* a field is not editable, not merely
   find it disabled." Each field definition may supply `readOnly(row)`,
   returning either `false` or a human-readable reason string; when a reason
   is returned the control is disabled AND the reason is rendered as visible
   text next to it (not only a title attribute, so it is available without
   hover).
*/

import { h, text, clear } from '../../core/dom.js';
import { SettingsApiError } from '../../features/settings/api.js';

/**
 * @param {Object} opts
 * @param {Array<{
 *   key: string, label: string, type?: 'text'|'checkbox'|'textarea',
 *   required?: boolean, hint?: string,
 *   readOnly?: (row: Object|null) => (false|string),
 *   render?: (value: any, row: Object|null) => Node,
 * }>} opts.formFields
 */
export function createRecordDialog({ formFields }) {
  const titleEl = h('h2', { id: 'settingsDlgTitle' }, '');
  const closeBtn = h('button', {
    type: 'button', class: 'dlg-close', 'aria-label': 'Close dialog',
    onClick: () => dialogEl.close(),
  }, '✕');
  const head = h('div', { class: 'dlg-head' }, [titleEl, closeBtn]);

  const errorSummary = h('div', { class: 'msg msg-error', role: 'alert', hidden: true });
  const conflictBanner = h('div', { class: 'msg msg-warning', role: 'alert', hidden: true });
  const msgHost = h('div', { class: 'dlg-msg' }, [conflictBanner, errorSummary]);

  const fieldEls = new Map();
  const fieldsBody = h('div', { class: 'dlg-body' });

  for (const f of formFields) {
    const type = f.type || 'text';
    const fieldId = `settingsField_${f.key}`;
    const errId = `${fieldId}_err`;
    let control;
    if (type === 'checkbox') {
      control = h('input', { type: 'checkbox', id: fieldId, name: f.key });
    } else if (type === 'textarea') {
      control = h('textarea', { id: fieldId, name: f.key, rows: '3', 'aria-describedby': errId });
    } else {
      control = h('input', { type: 'text', id: fieldId, name: f.key, 'aria-describedby': errId });
    }
    if (f.required) control.setAttribute('aria-required', 'true');
    const errEl = h('div', { id: errId, class: 'field-err xs st-negative', hidden: true });
    const readonlyNote = h('div', { class: 'field-readonly-note xs' }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, '🔒'),
      h('span', { class: 'note-text' }, ''),
    ]);
    readonlyNote.hidden = true;
    const hintEl = f.hint ? h('div', { class: 'field-hint xs muted' }, f.hint) : null;

    const wrap = type === 'checkbox'
      ? h('div', { class: 'field check' }, [control, h('label', { for: fieldId }, f.label)])
      : h('div', { class: 'field' }, [
        h('label', { for: fieldId }, f.label + (f.required ? ' *' : '')),
        control, hintEl, readonlyNote, errEl,
      ].filter(Boolean));

    fieldEls.set(f.key, {
      def: f, control, errEl, readonlyNote, wrap,
    });
    fieldsBody.appendChild(wrap);
  }

  const submitBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Save');
  const cancelBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => dialogEl.close() }, 'Cancel');
  const reloadBtn = h('button', {
    type: 'button',
    class: 'btn-sm',
    onClick: () => reloadLatest(),
  }, 'Reload latest values');
  const foot = h('div', { class: 'dlg-foot' }, [cancelBtn, submitBtn]);

  // The footer's Save button must be a genuine descendant of the <form> to
  // trigger its submit event (a `type="submit"` button outside its form's
  // subtree submits nothing) — so the form wraps the message host, the
  // fields and the footer together, rather than sitting beside them.
  const form = h('form', { novalidate: true }, [msgHost, fieldsBody, foot]);

  const dialogEl = h('dialog', { 'aria-labelledby': 'settingsDlgTitle' }, [head, form]);

  let session = null; // { mode, row, onSubmit, onReload }
  let lastFocused = null;

  function setFieldError(key, message) {
    const f = fieldEls.get(key);
    if (!f) return;
    f.errEl.hidden = !message;
    clear(f.errEl);
    if (message) f.errEl.appendChild(text(message));
    f.control.setAttribute('aria-invalid', message ? 'true' : 'false');
  }

  function clearAllErrors() {
    for (const key of fieldEls.keys()) setFieldError(key, null);
    errorSummary.hidden = true;
    clear(errorSummary);
    conflictBanner.hidden = true;
    clear(conflictBanner);
  }

  function applyReadOnly(row) {
    for (const { def, control, readonlyNote } of fieldEls.values()) {
      const reason = def.readOnly ? def.readOnly(row) : false;
      control.disabled = !!reason;
      readonlyNote.hidden = !reason;
      if (reason) {
        const noteText = readonlyNote.querySelector('.note-text');
        clear(noteText);
        noteText.appendChild(text(reason));
      }
    }
  }

  function fillForm(values) {
    for (const [key, { def, control }] of fieldEls) {
      const value = values ? values[key] : undefined;
      if (def.type === 'checkbox') {
        control.checked = value === undefined ? true : !!value;
      } else {
        control.value = value === undefined || value === null ? '' : String(value);
      }
    }
  }

  function readValues() {
    const values = {};
    for (const [key, { def, control }] of fieldEls) {
      if (control.disabled) continue; // never submit a value the user could not see was locked
      values[key] = def.type === 'checkbox' ? control.checked : control.value.trim();
    }
    return values;
  }

  function validate(values) {
    const invalidKeys = [];
    for (const [key, { def, control }] of fieldEls) {
      if (control.disabled) continue;
      if (def.required && (values[key] === '' || values[key] === undefined)) {
        setFieldError(key, `${def.label} is required.`);
        invalidKeys.push(key);
      } else {
        setFieldError(key, null);
      }
    }
    return invalidKeys;
  }

  async function reloadLatest() {
    if (!session || !session.onReload) return;
    conflictBanner.hidden = true;
    try {
      const fresh = await session.onReload();
      session.row = fresh;
      fillForm(fresh);
      applyReadOnly(fresh);
    } catch {
      // If the reload itself fails, the conflict banner already told the
      // user what to do (reload the page); leaving the banner visible here
      // is more honest than pretending the reload silently succeeded.
    }
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    if (!session) return;
    clearAllErrors();
    const values = readValues();
    const invalid = validate(values);
    if (invalid.length) {
      errorSummary.hidden = false;
      clear(errorSummary);
      errorSummary.appendChild(h('div', {}, [
        h('strong', {}, 'Fix the following before saving: '),
        h('ul', { class: 'field-err-list' }, invalid.map((key) => h('li', {}, [
          h('a', {
            href: `#settingsField_${key}`,
            onClick: (e) => { e.preventDefault(); fieldEls.get(key).control.focus(); },
          }, fieldEls.get(key).def.label),
        ]))),
      ]));
      fieldEls.get(invalid[0]).control.focus();
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = 'Saving…';
    try {
      const payload = session.mode === 'edit' && session.row && session.row.version_no !== undefined
        ? { ...values, version_no: session.row.version_no }
        : values;
      const result = await session.onSubmit(payload, session.row);
      submitBtn.disabled = false;
      submitBtn.textContent = 'Save';
      dialogEl.close();
      if (session.onSuccess) session.onSuccess(result);
    } catch (err) {
      submitBtn.disabled = false;
      submitBtn.textContent = 'Save';
      if (err instanceof SettingsApiError && err.kind === 'conflict') {
        conflictBanner.hidden = false;
        clear(conflictBanner);
        conflictBanner.appendChild(h('div', {}, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
          h('div', { class: 'body' }, [
            h('strong', {}, 'Someone else changed this record. '),
            text(err.message || 'Reload the latest values before saving again, or your change will be rejected.'),
            h('div', { class: 'btn-row' }, [reloadBtn]),
          ]),
        ]));
      } else {
        errorSummary.hidden = false;
        clear(errorSummary);
        errorSummary.appendChild(h('div', {}, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
          h('div', { class: 'body' }, [
            text(err instanceof SettingsApiError ? err.message : 'This record could not be saved. Try again.'),
            err instanceof SettingsApiError && err.messageId ? h('div', { class: 'mid' }, err.messageId) : null,
          ].filter(Boolean)),
        ]));
      }
    }
  });

  dialogEl.addEventListener('close', () => {
    session = null;
    if (lastFocused && typeof lastFocused.focus === 'function') lastFocused.focus();
  });

  /**
   * @param {Object} args
   * @param {'create'|'edit'} args.mode
   * @param {string} args.title
   * @param {Object|null} [args.row] - existing values (edit) or defaults (create).
   * @param {(values: Object, row: Object|null) => Promise<any>} args.onSubmit
   * @param {(result: any) => void} [args.onSuccess]
   * @param {() => Promise<Object>} [args.onReload] - edit mode: re-fetch the row after a 409.
   * @param {HTMLElement} [args.opener] - element to restore focus to on close.
   */
  function open({
    mode, title, row = null, onSubmit, onSuccess, onReload, opener,
  }) {
    session = {
      mode, row, onSubmit, onSuccess, onReload,
    };
    lastFocused = opener || document.activeElement;
    clearAllErrors();
    titleEl.textContent = title;
    fillForm(row || {});
    applyReadOnly(mode === 'edit' ? row : null);
    submitBtn.textContent = 'Save';
    if (!dialogEl.isConnected) document.body.appendChild(dialogEl);
    dialogEl.showModal();
    const first = [...fieldEls.values()].find(({ control }) => !control.disabled);
    if (first) first.control.focus();
  }

  return { el: dialogEl, open, close: () => dialogEl.close() };
}
