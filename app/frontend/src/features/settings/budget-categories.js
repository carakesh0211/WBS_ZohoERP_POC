/* app/frontend/src/features/settings/budget-categories.js
   SCR "Budget Categories" — the budget-category master (Fable 5.1,
   migration 026), read via app/backend/api/budgets_original.py and its
   pg/original_budget.py service. Modelled on
   features/settings/masters.js's list / create-edit / deactivate shape,
   with the client narrowed to this feature's own
   features/budget/budget-api.js rather than features/settings/api.js —
   /api/budget/categories is a budget-router route, not a masters one, even
   though this screen lives in the Settings and Governance area of the app.

   Categories are DEACTIVATED, never deleted (the same convention
   masters.js's deactivate dialog documents), and carry an optional parent
   for a two-level hierarchy — chosen through a <governed-select
   kind="budget_category">, the same governed picker budget-setup.js uses
   for every line's own category, so a category can never name a parent by
   typing a raw id.

   Write controls (New category, Edit, Deactivate) render only when this
   session holds budget.category.manage; that gate is presentational, the
   server enforces it on every write route, per components/budget/
   budget-api.js::getPrincipal's own documented caveat.
*/

import { h, text, clear } from '../../core/dom.js';
import {
  listCategories, createCategory, updateCategory, deactivateCategory, getPrincipal, BudgetApiError,
} from '../budget/budget-api.js';
import { formatSettingsDate } from './format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import '../../components/budget/governed-select.js';

function msgBox(kind, body, { messageId, alert = false, actions = [] } = {}) {
  const icon = { error: '✖', warning: '!', success: '✔', info: '·' }[kind] || '·';
  const attrs = { class: `msg msg-${kind}` };
  if (alert) attrs.role = 'alert';
  return h('div', attrs, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, icon),
    h('div', { class: 'body' }, [
      body,
      actions.length ? h('div', { class: 'actions' }, actions) : null,
      messageId ? h('div', { class: 'mid' }, messageId) : null,
    ].filter(Boolean)),
  ]);
}

function activeChip(isActive) {
  return isActive ? statusChip({ label: 'Active', tone: 'positive' }) : statusChip({ label: 'Inactive', tone: 'neutral' });
}

/** Same envelope-unwrap reasoning as budget-setup.js::problemDetail — see
 * that file's comment; app/backend/api/budget.py::_problem() nests the
 * human-readable text under body.detail.detail, one level below what
 * core/api-client.js::readProblem() unwraps. */
function problemDetail(err) {
  const raw = err && err.body;
  const nested = raw && typeof raw === 'object' && raw.detail && typeof raw.detail === 'object' ? raw.detail : null;
  const message = (nested && typeof nested.detail === 'string' && nested.detail.trim()) ? nested.detail : ((err && err.message) || 'This request could not be completed.');
  return { message };
}

export function mountBudgetCategories(root) {
  if (!root) return;
  const liveRegion = document.getElementById('budgetCategoriesLiveRegion') || document.getElementById('budgetLiveRegion');
  function announce(message) { if (liveRegion) liveRegion.textContent = message; }

  const state = {
    permissions: new Set(),
    includeInactive: false,
    items: [],
    listState: 'loading', // loading|empty|error|permission|ready
    listError: null,
  };
  function can(permission) { return state.permissions.has(permission); }

  /* ---------------- toolbar ---------------- */

  const includeInactiveCheckbox = h('input', { type: 'checkbox', id: 'budgetCategoriesIncludeInactive' });
  const newBtn = h('button', { type: 'button', class: 'btn-primary btn-sm', hidden: true, onClick: (ev) => openCreate(ev.currentTarget) }, '+ New category');

  const toolbar = h('form', {
    class: 'toolbar',
    onSubmit: (ev) => { ev.preventDefault(); state.includeInactive = includeInactiveCheckbox.checked; loadList(); },
  }, [
    h('div', { class: 'field check' }, [includeInactiveCheckbox, h('label', { for: 'budgetCategoriesIncludeInactive' }, 'Include inactive')]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), h('button', { type: 'submit', class: 'btn-sm' }, 'Apply')]),
    h('div', { class: 'field field-action grow-action' }, [h('span', { class: 'sr-only' }, ''), newBtn]),
  ]);
  includeInactiveCheckbox.addEventListener('change', () => { state.includeInactive = includeInactiveCheckbox.checked; loadList(); });

  const statusHost = h('div', { id: 'budgetCategoriesStatus' });

  function categoryLabel(id) {
    if (!id) return '—';
    const row = state.items.find((r) => r.category_id === id);
    return row ? `${row.code} — ${row.name}` : id;
  }

  const table = createDataTable({
    caption: 'Budget categories — code, name, parent and status',
    emptyMessage: 'No budget categories match these filters.',
    columns: [
      { key: 'code', label: 'Code', render: (row) => h('span', { class: 'mono' }, row.code || '—') },
      { key: 'name', label: 'Name', render: (row) => text(row.name || '—') },
      { key: 'parent_category_id', label: 'Parent', render: (row) => text(categoryLabel(row.parent_category_id)) },
      { key: 'display_order', label: 'Order', numeric: true, render: (row) => text(String(row.display_order ?? '—')) },
      { key: 'status', label: 'Status', render: (row) => activeChip(row.active) },
      {
        key: 'effective',
        label: 'Effective',
        render: (row) => text(`${formatSettingsDate(row.effective_from)} – ${row.effective_to ? formatSettingsDate(row.effective_to) : 'open'}`),
      },
      {
        key: 'actions',
        label: 'Actions',
        render: (row) => h('div', { class: 'row-actions' }, [
          can('budget.category.manage')
            ? h('button', { type: 'button', class: 'btn-sm', onClick: (ev) => openEdit(row, ev.currentTarget) }, 'Edit')
            : null,
          can('budget.category.manage') && row.active
            ? h('button', { type: 'button', class: 'btn-sm btn-danger', onClick: (ev) => openDeactivate(row, ev.currentTarget) }, 'Deactivate')
            : null,
        ].filter(Boolean)),
      },
    ],
  });

  root.appendChild(h('h2', { class: 'sr-only' }, 'Budget categories'));
  root.appendChild(toolbar);
  root.appendChild(statusHost);
  root.appendChild(table.el);

  function renderList() {
    clear(statusHost);
    if (state.listState === 'loading') {
      statusHost.hidden = true; table.el.hidden = false; table.renderSkeleton(); return;
    }
    if (state.listState === 'permission') {
      statusHost.hidden = false; table.el.hidden = true; table.renderRows([]);
      statusHost.appendChild(h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '⚙'),
        h('div', {}, state.listError ? state.listError.message : 'No budget categories were found.'),
      ]));
      return;
    }
    if (state.listState === 'error') {
      statusHost.hidden = false; table.el.hidden = true; table.renderRows([]);
      const err = state.listError;
      statusHost.appendChild(msgBox('error', h('div', {}, err ? err.message : 'Budget categories could not be loaded.'), {
        messageId: err && err.messageId, alert: true,
        actions: [h('button', { type: 'button', class: 'btn-sm', onClick: () => loadList() }, 'Retry')],
      }));
      return;
    }
    if (state.listState === 'empty') {
      statusHost.hidden = false; table.el.hidden = true; table.renderRows([]);
      statusHost.appendChild(h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '⚙'),
        h('div', {}, can('budget.category.manage')
          ? 'No budget categories match these filters. Use "New category" above to create one.'
          : 'No budget categories match these filters.'),
      ]));
      return;
    }
    statusHost.hidden = true; table.el.hidden = false; table.renderRows(state.items);
  }

  async function loadList() {
    state.listState = 'loading'; state.listError = null; renderList();
    try {
      const data = await listCategories({ includeInactive: state.includeInactive });
      state.items = (data && data.items) || [];
      state.listState = state.items.length ? 'ready' : 'empty';
      announce(state.listState === 'ready' ? `${state.items.length} budget categories loaded.` : 'No budget categories match these filters.');
    } catch (err) {
      if (err instanceof BudgetApiError && err.kind === 'notfound') {
        state.listState = 'permission';
        state.listError = { message: err.message, messageId: err.messageId };
      } else {
        state.listState = 'error';
        state.listError = { message: err instanceof BudgetApiError ? err.message : 'Budget categories could not be loaded. Try again.', messageId: err instanceof BudgetApiError ? err.messageId : null };
      }
      announce('Budget categories could not be loaded.');
    }
    renderList();
  }

  /* ---------------- create / edit dialog ---------------- */

  const dlgTitleEl = h('h2', { id: 'budgetCategoryDlgTitle' }, '');
  const dlgClose = h('button', { type: 'button', class: 'dlg-close', 'aria-label': 'Close dialog', onClick: () => dialogEl.close() }, '✕');
  const dlgErrorSummary = h('div', { class: 'msg msg-error', role: 'alert', hidden: true });
  const dlgConflictBanner = h('div', { class: 'msg msg-warning', role: 'alert', hidden: true });
  const dlgMsgHost = h('div', { class: 'dlg-msg' }, [dlgConflictBanner, dlgErrorSummary]);

  const codeInput = h('input', { type: 'text', id: 'budgetCategoryCode', required: true });
  const codeErr = h('div', { class: 'field-err xs st-negative', hidden: true });
  const nameInput = h('input', { type: 'text', id: 'budgetCategoryName', required: true });
  const nameErr = h('div', { class: 'field-err xs st-negative', hidden: true });
  const descInput = h('textarea', { id: 'budgetCategoryDesc', rows: '2' });
  const parentEl = document.createElement('governed-select');
  parentEl.setAttribute('kind', 'budget_category');
  parentEl.setAttribute('placeholder', 'Search parent category…');
  parentEl.setAttribute('aria-labelledby', 'budgetCategoryParentLabel');
  const parentErr = h('div', { class: 'field-err xs st-negative', hidden: true });
  const orderInput = h('input', { type: 'text', inputmode: 'numeric', id: 'budgetCategoryOrder' });
  const effFromInput = h('input', { type: 'date', id: 'budgetCategoryEffFrom' });
  const effToInput = h('input', { type: 'date', id: 'budgetCategoryEffTo' });

  const dlgFields = h('div', { class: 'dlg-body' }, [
    h('div', { class: 'field' }, [h('label', { for: 'budgetCategoryCode' }, 'Code *'), codeInput, codeErr]),
    h('div', { class: 'field' }, [h('label', { for: 'budgetCategoryName' }, 'Name *'), nameInput, nameErr]),
    h('div', { class: 'field' }, [h('label', { for: 'budgetCategoryDesc' }, 'Description'), descInput]),
    h('div', { class: 'field' }, [h('label', { id: 'budgetCategoryParentLabel' }, 'Parent category'), parentEl, parentErr]),
    h('div', { class: 'field' }, [h('label', { for: 'budgetCategoryOrder' }, 'Display order'), orderInput]),
    h('div', { class: 'field' }, [h('label', { for: 'budgetCategoryEffFrom' }, 'Effective from'), effFromInput]),
    h('div', { class: 'field' }, [h('label', { for: 'budgetCategoryEffTo' }, 'Effective to'), effToInput]),
  ]);

  const dlgSubmitBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Save');
  const dlgCancelBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => dialogEl.close() }, 'Cancel');
  const dlgReloadBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => reloadConflict() }, 'Reload latest values');
  const dlgFoot = h('div', { class: 'dlg-foot' }, [dlgCancelBtn, dlgSubmitBtn]);

  const dlgForm = h('form', { novalidate: true }, [dlgMsgHost, dlgFields, dlgFoot]);
  const dialogEl = h('dialog', { 'aria-labelledby': 'budgetCategoryDlgTitle' }, [
    h('div', { class: 'dlg-head' }, [dlgTitleEl, dlgClose]), dlgForm,
  ]);

  let dlgMode = 'create';
  let dlgRow = null;
  let dlgOpener = null;

  function clearDlgErrors() {
    codeErr.hidden = true; clear(codeErr);
    nameErr.hidden = true; clear(nameErr);
    parentErr.hidden = true; clear(parentErr);
    dlgErrorSummary.hidden = true; clear(dlgErrorSummary);
    dlgConflictBanner.hidden = true; clear(dlgConflictBanner);
  }

  function fillDlg(row) {
    codeInput.value = row ? (row.code || '') : '';
    codeInput.disabled = !!row; // code is immutable after creation — the API accepts no `code` on PUT
    nameInput.value = row ? (row.name || '') : '';
    descInput.value = row ? (row.description || '') : '';
    if (row && row.parent_category_id) parentEl.presetSelection(row.parent_category_id, categoryLabel(row.parent_category_id));
    else parentEl.clear();
    orderInput.value = row && row.display_order !== undefined && row.display_order !== null ? String(row.display_order) : '100';
    effFromInput.value = row && row.effective_from ? String(row.effective_from).slice(0, 10) : '';
    effToInput.value = row && row.effective_to ? String(row.effective_to).slice(0, 10) : '';
  }

  function openCreate(opener) {
    dlgMode = 'create'; dlgRow = null; dlgOpener = opener || document.activeElement;
    clearDlgErrors();
    dlgTitleEl.textContent = 'New budget category';
    // Connect the dialog (and its governed-select parentEl) to the document
    // BEFORE filling it -- fillDlg() presets/clears parentEl, and a
    // governed-select's presetSelection()/clear() touch its internal input,
    // which only exists once connectedCallback() has built it.
    if (!dialogEl.isConnected) document.body.appendChild(dialogEl);
    fillDlg(null);
    dlgSubmitBtn.textContent = 'Save';
    dialogEl.showModal();
    codeInput.focus();
  }

  function openEdit(row, opener) {
    dlgMode = 'edit'; dlgRow = row; dlgOpener = opener || document.activeElement;
    clearDlgErrors();
    dlgTitleEl.textContent = `Edit ${row.code || ''}`.trim();
    if (!dialogEl.isConnected) document.body.appendChild(dialogEl);
    fillDlg(row);
    dlgSubmitBtn.textContent = 'Save';
    dialogEl.showModal();
    nameInput.focus();
  }

  async function reloadConflict() {
    if (!dlgRow) return;
    dlgConflictBanner.hidden = true;
    try {
      const data = await listCategories({ includeInactive: true });
      const fresh = ((data && data.items) || []).find((r) => r.category_id === dlgRow.category_id);
      if (fresh) { dlgRow = fresh; fillDlg(fresh); }
    } catch { /* the banner already told the user what to do */ }
  }

  dialogEl.addEventListener('close', () => {
    if (dlgOpener && typeof dlgOpener.focus === 'function') dlgOpener.focus();
    dlgOpener = null;
  });

  dlgForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    clearDlgErrors();
    let firstInvalid = null;
    const code = codeInput.value.trim();
    const name = nameInput.value.trim();
    if (dlgMode === 'create' && !code) {
      codeErr.hidden = false; clear(codeErr); codeErr.appendChild(text('Code is required.'));
      firstInvalid = codeInput;
    }
    if (!name) {
      nameErr.hidden = false; clear(nameErr); nameErr.appendChild(text('Name is required.'));
      firstInvalid = firstInvalid || nameInput;
    }
    if (parentEl.value === (dlgRow && dlgRow.category_id)) {
      parentErr.hidden = false; clear(parentErr); parentErr.appendChild(text('A category cannot be its own parent.'));
      firstInvalid = firstInvalid || parentEl;
    }
    if (firstInvalid) { firstInvalid.focus(); return; }

    const displayOrder = orderInput.value.trim() ? parseInt(orderInput.value.trim(), 10) : undefined;
    dlgSubmitBtn.disabled = true; dlgSubmitBtn.textContent = 'Saving…';
    try {
      if (dlgMode === 'create') {
        await createCategory({
          code, name, description: descInput.value.trim() || undefined,
          parent_category_id: parentEl.value || undefined,
          display_order: displayOrder,
          effective_from: effFromInput.value || undefined,
          effective_to: effToInput.value || undefined,
        });
        announce(`Category ${code} created.`);
      } else {
        await updateCategory(dlgRow.category_id, {
          expected_version: dlgRow.version_no,
          name, description: descInput.value.trim() || undefined,
          parent_category_id: parentEl.value || undefined,
          display_order: displayOrder,
          effective_from: effFromInput.value || undefined,
          effective_to: effToInput.value || undefined,
        });
        announce(`Category ${dlgRow.code} updated.`);
      }
      dialogEl.close();
      loadList();
    } catch (err) {
      if (err instanceof BudgetApiError && err.status === 409 && err.code === 'CATEGORY_STALE') {
        dlgConflictBanner.hidden = false;
        clear(dlgConflictBanner);
        dlgConflictBanner.appendChild(h('div', {}, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
          h('div', { class: 'body' }, [
            h('strong', {}, 'Someone else changed this category. '),
            text('Reload the latest values before saving again, or your change will be rejected.'),
            h('div', { class: 'btn-row' }, [dlgReloadBtn]),
          ]),
        ]));
      } else {
        const message = err instanceof BudgetApiError ? problemDetail(err).message : 'This category could not be saved. Try again.';
        dlgErrorSummary.hidden = false;
        clear(dlgErrorSummary);
        dlgErrorSummary.appendChild(h('div', {}, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
          h('div', { class: 'body' }, message),
        ]));
      }
    } finally {
      dlgSubmitBtn.disabled = false; dlgSubmitBtn.textContent = 'Save';
    }
  });

  /* ---------------- deactivate confirmation ---------------- */

  const confirmTitleEl = h('h2', { id: 'budgetCategoryConfirmTitle' }, '');
  const confirmClose = h('button', { type: 'button', class: 'dlg-close', 'aria-label': 'Close dialog', onClick: () => confirmDialogEl.close() }, '✕');
  const confirmBody = h('div', { class: 'dlg-body' });
  const confirmErrorHost = h('div', { class: 'dlg-msg' });
  const confirmBtn = h('button', { type: 'button', class: 'btn-danger btn-sm', onClick: () => runDeactivate() }, 'Deactivate');
  const confirmCancelBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => confirmDialogEl.close() }, 'Cancel');
  const confirmDialogEl = h('dialog', { 'aria-labelledby': 'budgetCategoryConfirmTitle' }, [
    h('div', { class: 'dlg-head' }, [confirmTitleEl, confirmClose]),
    confirmErrorHost, confirmBody,
    h('div', { class: 'dlg-foot' }, [confirmCancelBtn, confirmBtn]),
  ]);
  let confirmRow = null;
  let confirmOpener = null;

  confirmDialogEl.addEventListener('close', () => {
    if (confirmOpener && typeof confirmOpener.focus === 'function') confirmOpener.focus();
    confirmOpener = null;
  });

  function openDeactivate(row, opener) {
    confirmRow = row; confirmOpener = opener || document.activeElement;
    clear(confirmErrorHost);
    confirmTitleEl.textContent = `Deactivate ${row.code || ''}`.trim();
    clear(confirmBody);
    confirmBody.appendChild(text(`${row.name || row.code} will be marked inactive and hidden from active pickers elsewhere in the `
      + 'application (including every governed-select on the Budget Setup screen). This does not delete it or its '
      + 'history, and the server refuses this while an active child category exists or while a draft, submitted or '
      + 'returned original budget still names it.'));
    if (!confirmDialogEl.isConnected) document.body.appendChild(confirmDialogEl);
    confirmDialogEl.showModal();
    confirmBtn.focus();
  }

  async function runDeactivate() {
    if (!confirmRow) return;
    clear(confirmErrorHost);
    confirmBtn.disabled = true; confirmBtn.textContent = 'Deactivating…';
    try {
      await deactivateCategory(confirmRow.category_id);
      confirmDialogEl.close();
      announce(`Category ${confirmRow.code} deactivated.`);
      loadList();
    } catch (err) {
      const message = err instanceof BudgetApiError ? problemDetail(err).message : 'This category could not be deactivated. Try again.';
      confirmErrorHost.appendChild(h('div', { class: 'msg msg-error', role: 'alert' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
        h('div', { class: 'body' }, message),
      ]));
    } finally {
      confirmBtn.disabled = false; confirmBtn.textContent = 'Deactivate';
    }
  }

  /* ---------------- boot ---------------- */

  (async function boot() {
    const principal = await getPrincipal();
    state.permissions = principal.permissions;
    newBtn.hidden = !can('budget.category.manage');
    loadList();
  }());
}
