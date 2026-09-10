/* app/frontend/src/features/budget/budget-setup.js
   "Budget Setup" — create, edit, submit, cancel and import an ORIGINAL
   BUDGET document, and manage the budget-category master used to classify
   its lines. Wired to app/backend/api/budgets_original.py (see the module
   docstring copied into budget-api.js above the endpoint functions this
   screen calls).

   Screen shape
   ------------
   Two top-level tabs: "Budgets" (list + document editor) and "Upload
   budget" (CSV template / preview / all-or-nothing import). The list and
   the editor share the "Budgets" tab as two modes of the same panel, the
   same way capex-record-dialog's create/edit modes share one dialog rather
   than being two screens.

   Every selector a line carries — WBS, Budget Head, Budget Category, and
   the optional division/branch/zone/plant/location — is a
   <governed-select>, never a typed id: components/budget/governed-select.js
   is the only way any of those ids reach the payload.

   Money is integer paise everywhere. Every amount field is read through
   components/budget/money-input.js::parseRupeesToPaise, which throws a
   user-facing string rather than ever routing an amount through a float.

   No static fake data: every figure on screen came from a fetch() call.
*/

import {
  listOriginals, getOriginal, createOriginal, updateOriginal, submitOriginal, cancelOriginal,
  getOriginalAudit, listCustomFields, getImportTemplate, previewImport, commitImport,
  BudgetApiError,
} from './budget-api.js';
import { h, text, clear } from '../../core/dom.js';
import { formatINR, formatAuditTimestamp } from '../../core/format.js';
import { parseRupeesToPaise } from '../../components/budget/money-input.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { createReasonDialog } from '../../components/approvals/reason-dialog.js';
import { newCorrelationId } from '../../core/api-client.js';
import '../../components/budget/governed-select.js';

const PAGE_SIZE = 50;

const STATUS_TONE = {
  DRAFT: 'neutral', SUBMITTED: 'progress', RELEASED: 'positive',
  REJECTED: 'negative', RETURNED: 'warning', CANCELLED: 'neutral',
};
const EDITABLE_STATUSES = new Set(['DRAFT', 'RETURNED']);

/* ---------------- shared small helpers ---------------- */

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

function emptyBlock(message, glyph = '▩') {
  return h('div', { class: 'empty' }, [
    h('div', { class: 'big', 'aria-hidden': 'true' }, glyph),
    h('div', {}, message),
  ]);
}

/**
 * Read the ACTUAL message and, when present, the per-line problem array out
 * of a BudgetApiError's raw body.
 *
 * app/backend/api/budget.py::_problem() nests the human-readable text under
 * `body.detail.detail` (RFC-7807's own `detail` field, one level below the
 * FastAPI `HTTPException(detail={...})` envelope), which
 * core/api-client.js::readProblem() does not unwrap — it looks for
 * `body.detail.message`. Rather than edit that shared, frozen module for one
 * caller, the real message and the 422 BUDGET_LINES_INVALID `problems` array
 * are read directly off the error's preserved raw `body` here.
 */
function problemDetail(err) {
  const raw = err && err.body;
  const nested = raw && typeof raw === 'object' && raw.detail && typeof raw.detail === 'object' ? raw.detail : null;
  const message = (nested && typeof nested.detail === 'string' && nested.detail.trim())
    ? nested.detail
    : ((err && err.message) || 'This request could not be completed.');
  const problems = (nested && Array.isArray(nested.problems)) ? nested.problems : [];
  return { message, problems };
}

/** The session's held permissions, for PRESENTATIONAL gating only — the
 * server is the enforcement point for every write this screen offers.
 * app.js is a classic script; its top-level `const S` lives in the global
 * lexical environment, on the scope chain of a module evaluated in the same
 * realm, so the shell's own answer is read directly rather than issuing a
 * second /api/bootstrap (which returns every project, entity and user in
 * the installation to answer a permission question). Guarded so this module
 * never REQUIRES the shell to exist. */
async function getPrincipal() {
  try {
    // eslint-disable-next-line no-undef
    const shell = typeof S !== 'undefined' ? S : null;
    if (shell && shell.perms && typeof shell.perms.has === 'function' && shell.perms.size) {
      return { permissions: new Set([...shell.perms].map(String)) };
    }
  } catch { /* fall through */ }
  try {
    const res = await fetch('/api/bootstrap', {
      headers: (() => {
        try {
          const sid = sessionStorage.getItem('capex.session_id');
          return sid ? { 'X-Session': sid } : {};
        } catch { return {}; }
      })(),
    });
    const body = await res.json();
    return { permissions: new Set(((body && body.permissions) || []).map(String)) };
  } catch {
    return { permissions: new Set() };
  }
}

function uid() { return newCorrelationId(); }

/* ---------------- custom fields (shared shape at header AND line level) ----------------
   /api/budget/custom-fields returns ONE flat definition set (applies_to ==
   "BUDGET"), and app/backend/pg/original_budget.py::validate_custom_fields
   is called against BOTH the header's custom_fields and every line's
   custom_fields with the SAME defs — so the same control set is rendered in
   both places, each instance carrying its own values. */

function buildCustomFieldControl(def, idPrefix) {
  const fieldId = `${idPrefix}_${def.code}`;
  let control;
  if (def.data_type === 'BOOLEAN') {
    control = h('input', { type: 'checkbox', id: fieldId });
  } else if (def.data_type === 'DATE') {
    control = h('input', { type: 'date', id: fieldId });
  } else if (def.data_type === 'NUMBER') {
    control = h('input', { type: 'text', inputmode: 'decimal', id: fieldId });
  } else if (def.data_type === 'SELECT') {
    control = h('select', { id: fieldId }, [
      h('option', { value: '' }, '—'),
      ...((def.select_options || []).map((opt) => h('option', { value: opt }, opt))),
    ]);
  } else {
    control = h('input', { type: 'text', id: fieldId });
  }
  const errEl = h('div', { class: 'field-err xs st-negative', hidden: true });
  const wrap = def.data_type === 'BOOLEAN'
    ? h('div', { class: 'field check' }, [control, h('label', { for: fieldId }, def.label)])
    : h('div', { class: 'field' }, [
      h('label', { for: fieldId }, def.label + (def.is_required ? ' *' : '')), control, errEl,
    ]);
  return {
    def, control, errEl, wrap,
    get() {
      if (def.data_type === 'BOOLEAN') return control.checked;
      return control.value;
    },
    set(value) {
      if (def.data_type === 'BOOLEAN') control.checked = value === true || value === 'true';
      else control.value = value === undefined || value === null ? '' : String(value);
    },
    setError(message) {
      errEl.hidden = !message;
      clear(errEl);
      if (message) errEl.appendChild(text(message));
    },
  };
}

function buildCustomFieldsBlock(defs, idPrefix) {
  const controls = defs.map((def) => buildCustomFieldControl(def, idPrefix));
  const el = h('div', { class: 'budget-custom-fields' }, controls.map((c) => c.wrap));
  return {
    el,
    controls,
    values() {
      const out = {};
      for (const c of controls) {
        const v = c.get();
        if (v !== '' && v !== false) out[c.def.code] = v;
      }
      return out;
    },
    fill(values) {
      for (const c of controls) c.set((values || {})[c.def.code]);
    },
    clearErrors() { for (const c of controls) c.setError(null); },
    applyProblems(problems, where) {
      for (const p of problems || []) {
        const m = String(p.message || p.field || '').match(new RegExp(`${where}\\.(\\w+)`));
        if (m) {
          const c = controls.find((cc) => cc.def.code === m[1]);
          if (c) c.setError(p.message || 'Invalid value.');
        }
      }
    },
  };
}

/* ---------------- governed-select line field ---------------- */

let govFieldSeq = 0;

function govField(label, kind, { required = false, extraAttrs = {} } = {}) {
  govFieldSeq += 1;
  const labelId = `govFieldLabel${govFieldSeq}`;
  const el = document.createElement('governed-select');
  el.setAttribute('kind', kind);
  el.setAttribute('placeholder', `Search ${label.toLowerCase()}…`);
  el.setAttribute('aria-labelledby', labelId);
  if (required) el.setAttribute('required', '');
  for (const [k, v] of Object.entries(extraAttrs)) el.setAttribute(k, v);
  const errEl = h('div', { class: 'field-err xs st-negative', hidden: true });
  const labelEl = h('label', { id: labelId }, label + (required ? ' *' : ''));
  const wrap = h('div', { class: 'field' }, [labelEl, el, errEl]);
  return {
    el, wrap, errEl,
    setError(message) { errEl.hidden = !message; clear(errEl); if (message) errEl.appendChild(text(message)); },
  };
}

/* ==================================================================== */

export function mountBudgetSetup(root) {
  if (!root) return;
  const liveRegion = document.getElementById('budgetLiveRegion');
  function announce(message) { if (liveRegion) liveRegion.textContent = message; }

  const state = {
    permissions: new Set(),
    activeTab: 'budgets', // 'budgets' | 'upload'
    mode: 'list', // 'list' | 'editor'
    customFieldDefs: [],

    // list
    listState: 'loading', // loading|empty|error|permission|ready
    listError: null,
    items: [],
    cursor: null,
    hasMore: false,
    loadingMore: false,
    filterStatus: '',

    // editor
    doc: null, // last-saved server document, or null for an unsaved new draft
    docBusy: false,
    docBusyLabel: '',
    idemKey: uid(),
    lineEls: [], // [{ key, wrap, wbsSel, headSel, catSel, divSel, branchSel, zoneSel, plantSel, locSel,
                 //    amountInput, amountErrEl, justInput, customFields, removeBtn }]
    audit: { state: 'idle', entries: [] }, // 'idle' | 'loading' | 'ready' | 'error'

    // upload
    importProjectId: '',
    csvText: '',
    previewResult: null,
    previewState: 'idle', // 'idle' | 'loading' | 'ready' | 'error'
    previewError: null,
    importing: false,
    importIdemKey: uid(),
  };

  function can(permission) { return state.permissions.has(permission); }

  /* ---------------- top-level tabs ---------------- */

  const tabBudgets = h('button', {
    type: 'button', id: 'budgetSetupTabBudgets', class: 'budget-tabs-btn', role: 'tab',
    'aria-selected': 'true', 'aria-controls': 'budgetSetupPanelBudgets',
    onClick: () => selectTab('budgets'),
  }, 'Budgets');
  const tabUpload = h('button', {
    type: 'button', id: 'budgetSetupTabUpload', class: 'budget-tabs-btn', role: 'tab',
    'aria-selected': 'false', tabindex: '-1', 'aria-controls': 'budgetSetupPanelUpload',
    onClick: () => selectTab('upload'),
  }, 'Upload budget');
  const tablist = h('div', {
    class: 'budget-tabs', role: 'tablist', 'aria-label': 'Budget Setup sections',
    onKeydown: (ev) => {
      if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') return;
      ev.preventDefault();
      selectTab(state.activeTab === 'budgets' ? 'upload' : 'budgets');
      (state.activeTab === 'budgets' ? tabBudgets : tabUpload).focus();
    },
  }, [tabBudgets, tabUpload]);

  const panelBudgets = h('div', { id: 'budgetSetupPanelBudgets', role: 'tabpanel', 'aria-labelledby': 'budgetSetupTabBudgets' });
  const panelUpload = h('div', { id: 'budgetSetupPanelUpload', role: 'tabpanel', 'aria-labelledby': 'budgetSetupTabUpload', hidden: true });

  function selectTab(id) {
    state.activeTab = id;
    tabBudgets.setAttribute('aria-selected', String(id === 'budgets'));
    tabBudgets.tabIndex = id === 'budgets' ? 0 : -1;
    tabUpload.setAttribute('aria-selected', String(id === 'upload'));
    tabUpload.tabIndex = id === 'upload' ? 0 : -1;
    panelBudgets.hidden = id !== 'budgets';
    panelUpload.hidden = id !== 'upload';
  }

  root.appendChild(tablist);
  root.appendChild(panelBudgets);
  root.appendChild(panelUpload);

  /* ================================================================
     LIST
     ================================================================ */

  const listFilterProject = govField('Project', 'project');
  const filterStatusSelect = h('select', { id: 'budgetSetupStatusFilter' }, [
    h('option', { value: '' }, 'All statuses'),
    h('option', { value: 'DRAFT' }, 'Draft'),
    h('option', { value: 'SUBMITTED' }, 'Submitted'),
    h('option', { value: 'RELEASED' }, 'Released'),
    h('option', { value: 'REJECTED' }, 'Rejected'),
    h('option', { value: 'RETURNED' }, 'Returned'),
    h('option', { value: 'CANCELLED' }, 'Cancelled'),
  ]);
  const applyFiltersBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Apply filters');
  const clearFiltersBtn = h('button', {
    type: 'button', class: 'btn-sm btn-ghost',
    onClick: () => { listFilterProject.el.clear(); filterStatusSelect.value = ''; state.filterStatus = ''; loadList(true); },
  }, 'Clear filters');
  const newBudgetBtn = h('button', {
    type: 'button', class: 'btn-primary btn-sm', hidden: true,
    onClick: () => openNewDraft(),
  }, '+ New Budget');

  const listFilterForm = h('form', {
    class: 'toolbar budget-setup-toolbar',
    onSubmit: (ev) => {
      ev.preventDefault();
      state.filterStatus = filterStatusSelect.value;
      loadList(true);
    },
  }, [
    listFilterProject.wrap,
    h('div', { class: 'field' }, [h('label', { for: 'budgetSetupStatusFilter' }, 'Status'), filterStatusSelect]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), applyFiltersBtn]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, ''), clearFiltersBtn]),
    h('div', { class: 'field field-action grow-action' }, [h('span', { class: 'sr-only' }, ''), newBudgetBtn]),
  ]);
  listFilterProject.el.addEventListener('change', () => loadList(true));

  const listStatusHost = h('div', { id: 'budgetSetupListStatus' });
  const listTable = createDataTable({
    caption: 'Original budgets — number, project, fiscal year, title, status, total and line count',
    emptyMessage: 'No original budgets match these filters.',
    columns: [
      {
        key: 'budget_number',
        label: 'Budget',
        render: (row) => h('button', {
          type: 'button', class: 'btn-sm linkish',
          onClick: () => openExisting(row.budget_id),
        }, row.budget_number || row.budget_id),
      },
      { key: 'project_name', label: 'Project', render: (row) => text(`${row.capex_code || ''} — ${row.project_name || ''}`.replace(/^— /, '')) },
      { key: 'fiscal_year', label: 'Fiscal year', render: (row) => text(row.fiscal_year || '—') },
      { key: 'title', label: 'Title', render: (row) => text(row.title || '—') },
      { key: 'status', label: 'Status', render: (row) => statusChip({ label: row.status || 'Unknown', tone: STATUS_TONE[row.status] || 'neutral' }) },
      { key: 'total_paise', label: 'Total', numeric: true, render: (row) => h('span', { class: 'mono' }, formatINR(row.total_paise)) },
      { key: 'line_count', label: 'Lines', numeric: true, render: (row) => text(String(row.line_count ?? '—')) },
    ],
  });
  const listLoadMoreBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => loadList(false) }, 'Load more');
  const listPaginationInfo = h('span', { class: 'muted small' }, '');
  const listPagination = h('div', { class: 'settings-pagination', hidden: true }, [listPaginationInfo, h('span', { class: 'grow' }), listLoadMoreBtn]);

  const listView = h('div', { id: 'budgetSetupListView' }, [
    listFilterForm, listStatusHost, listTable.el, listPagination,
  ]);
  const editorView = h('div', { id: 'budgetSetupEditorView', hidden: true });
  panelBudgets.appendChild(listView);
  panelBudgets.appendChild(editorView);

  function renderList() {
    clear(listStatusHost);
    if (state.listState === 'loading') {
      listStatusHost.hidden = true; listTable.el.hidden = false; listTable.renderSkeleton();
      listPagination.hidden = true; return;
    }
    if (state.listState === 'permission') {
      listStatusHost.hidden = false; listTable.el.hidden = true; listTable.renderRows([]);
      listStatusHost.appendChild(emptyBlock(state.listError ? state.listError.message : 'No original budgets were found for these filters.'));
      listPagination.hidden = true; return;
    }
    if (state.listState === 'error') {
      listStatusHost.hidden = false; listTable.el.hidden = true; listTable.renderRows([]);
      const err = state.listError;
      listStatusHost.appendChild(msgBox('error', h('div', {}, err ? err.message : 'Original budgets could not be loaded.'), {
        messageId: err && err.messageId, alert: true,
        actions: [h('button', { type: 'button', class: 'btn-sm', onClick: () => loadList(true) }, 'Retry')],
      }));
      listPagination.hidden = true; return;
    }
    if (state.listState === 'empty') {
      listStatusHost.hidden = false; listTable.el.hidden = true; listTable.renderRows([]);
      listStatusHost.appendChild(emptyBlock(can('budget.create')
        ? 'No original budgets match these filters. Use "New Budget" above to create one, or adjust the filters.'
        : 'No original budgets match these filters.'));
      listPagination.hidden = true; return;
    }
    listStatusHost.hidden = true; listTable.el.hidden = false; listTable.renderRows(state.items);
    listPagination.hidden = false;
    listPaginationInfo.textContent = `${state.items.length} ${state.items.length === 1 ? 'budget' : 'budgets'} loaded`
      + (state.hasMore ? '' : ' · all matching budgets loaded');
    listLoadMoreBtn.hidden = !state.hasMore;
    listLoadMoreBtn.disabled = state.loadingMore;
    listLoadMoreBtn.textContent = state.loadingMore ? 'Loading…' : 'Load more';
  }

  async function loadList(reset) {
    if (reset) { state.items = []; state.cursor = null; state.listState = 'loading'; state.listError = null; renderList(); }
    else { state.loadingMore = true; renderList(); }
    try {
      const data = await listOriginals({
        projectId: listFilterProject.el.value || undefined,
        status: state.filterStatus || undefined,
        cursor: reset ? undefined : state.cursor || undefined,
        limit: PAGE_SIZE,
      });
      const items = (data && data.items) || [];
      state.items = reset ? items : state.items.concat(items);
      state.cursor = data ? data.next_cursor : null;
      state.hasMore = !!state.cursor;
      state.listState = state.items.length ? 'ready' : 'empty';
      state.loadingMore = false;
      announce(state.listState === 'ready' ? `${state.items.length} original budgets loaded.` : 'No original budgets match these filters.');
    } catch (err) {
      state.loadingMore = false;
      if (err instanceof BudgetApiError && err.kind === 'notfound') {
        state.listState = 'permission';
        state.listError = { message: err.message, messageId: err.messageId };
      } else {
        state.listState = 'error';
        state.listError = { message: err instanceof BudgetApiError ? err.message : 'Original budgets could not be loaded. Try again.', messageId: err instanceof BudgetApiError ? err.messageId : null };
      }
      announce('Original budgets could not be loaded.');
    }
    renderList();
  }

  /* ================================================================
     EDITOR — header, custom fields, lines, actions, audit
     ================================================================ */

  const editorStatusHost = h('div', { id: 'budgetSetupEditorStatus' });
  const backToListBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => showList() }, '← Back to list');
  const editorHeadingEl = h('h2', { class: 'section-title' }, 'New budget');

  const projField = govField('Project', 'project', { required: true });
  const fyField = govField('Fiscal year', 'fiscal_year', { required: true });
  const periodField = govField('Accounting period', 'period');
  const titleInput = h('input', { type: 'text', id: 'budgetSetupTitle', required: true });
  const justificationInput = h('textarea', { id: 'budgetSetupJustification', rows: '2' });
  const titleErr = h('div', { class: 'field-err xs st-negative', hidden: true });

  const headerCustomFieldsHost = h('div', { id: 'budgetSetupHeaderCustomFields' });

  const docHeaderEl = h('div', { class: 'budget-doc-header' }, [
    projField.wrap,
    fyField.wrap,
    periodField.wrap,
    h('div', { class: 'field span-2' }, [h('label', { for: 'budgetSetupTitle' }, 'Title *'), titleInput, titleErr]),
    h('div', { class: 'field span-2' }, [h('label', { for: 'budgetSetupJustification' }, 'Justification'), justificationInput]),
  ]);

  projField.el.addEventListener('change', () => {
    const id = projField.el.value;
    for (const line of state.lineEls) {
      if (id) line.wbsSel.el.setAttribute('project-id', id);
      else line.wbsSel.el.removeAttribute('project-id');
    }
  });

  const linesHeading = h('h3', { class: 'section-title' }, 'Lines');
  const linesContainer = h('div', { class: 'budget-lines-wrap' });
  const addLineBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => { addLine(); } }, '+ Add line');

  const totalBar = h('div', { class: 'budget-total-bar' }, [
    h('span', { class: 'k' }, 'Running total'),
    h('span', { class: 'v', id: 'budgetSetupTotal' }, formatINR(0)),
  ]);

  const saveDraftBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => saveDraft() }, 'Save draft');
  const submitBtn = h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => doSubmit() }, 'Submit for approval');
  const cancelDocBtn = h('button', { type: 'button', class: 'btn-sm btn-danger', onClick: () => openCancelDialog() }, 'Cancel budget');
  const editorActions = h('div', { class: 'budget-doc-actions' }, [saveDraftBtn, submitBtn, cancelDocBtn]);

  const auditHeading = h('h3', { class: 'section-title' }, 'Audit history');
  const auditHost = h('div', { id: 'budgetSetupAuditHost' });

  editorView.appendChild(backToListBtn);
  editorView.appendChild(editorHeadingEl);
  editorView.appendChild(editorStatusHost);
  editorView.appendChild(docHeaderEl);
  editorView.appendChild(headerCustomFieldsHost);
  editorView.appendChild(linesHeading);
  editorView.appendChild(linesContainer);
  editorView.appendChild(addLineBtn);
  editorView.appendChild(totalBar);
  editorView.appendChild(editorActions);
  editorView.appendChild(auditHeading);
  editorView.appendChild(auditHost);

  const cancelReasonDialog = createReasonDialog({ requireReason: true });

  function showList() {
    state.mode = 'list';
    listView.hidden = false;
    editorView.hidden = true;
    loadList(true);
  }

  function showEditor() {
    state.mode = 'editor';
    listView.hidden = true;
    editorView.hidden = false;
  }

  /* ---------------- lines ---------------- */

  function addLine(prefill) {
    const key = uid();
    const wbsSel = govField('WBS element', 'wbs', { required: true });
    if (projField.el.value) wbsSel.el.setAttribute('project-id', projField.el.value);
    const headSel = govField('Budget head', 'budget_head', { required: true });
    const catSel = govField('Budget category', 'budget_category', { required: true });
    const divSel = govField('Division', 'division');
    const branchSel = govField('Branch', 'branch');
    const zoneSel = govField('Zone', 'zone');
    const plantSel = govField('Plant', 'plant');
    const locSel = govField('Location', 'location');

    const amountInput = h('input', {
      type: 'text', inputmode: 'decimal', autocomplete: 'off', placeholder: 'e.g. 25,00,000.00',
      onInput: () => recomputeTotal(),
    });
    const amountErrEl = h('div', { class: 'field-err xs st-negative', hidden: true });
    const justInput = h('textarea', { rows: '2' });

    const customFields = buildCustomFieldsBlock(state.customFieldDefs, `budgetLine_${key}`);

    const removeBtn = h('button', {
      type: 'button', class: 'btn-sm btn-danger',
      onClick: () => removeLine(key),
    }, 'Remove line');

    const lineNoEl = h('span', { class: 'line-no' }, '');
    const card = h('div', { class: 'budget-line-card' }, [
      h('div', { class: 'line-head' }, [lineNoEl, h('span', { class: 'spacer' }), removeBtn]),
      h('div', { class: 'budget-line-grid' }, [
        wbsSel.wrap, headSel.wrap, catSel.wrap,
        divSel.wrap, branchSel.wrap, zoneSel.wrap, plantSel.wrap, locSel.wrap,
        h('div', { class: 'field' }, [h('label', {}, 'Amount (₹) *'), amountInput, amountErrEl]),
        h('div', { class: 'field span-2' }, [h('label', {}, 'Justification'), justInput]),
      ]),
      customFields.el,
    ]);

    const entry = {
      key, wrap: card, wbsSel, headSel, catSel, divSel, branchSel, zoneSel, plantSel, locSel,
      amountInput, amountErrEl, justInput, customFields,
    };

    if (prefill) {
      if (prefill.wbs_id) wbsSel.el.presetSelection(prefill.wbs_id, `${prefill.wbs_code || ''} — ${prefill.wbs_description || ''}`.replace(/^— /, ''));
      if (prefill.budget_head_id) headSel.el.presetSelection(prefill.budget_head_id, prefill.budget_head_name || prefill.budget_head_id);
      if (prefill.budget_category_id) catSel.el.presetSelection(prefill.budget_category_id, prefill.budget_category_name ? `${prefill.budget_category_code || ''} — ${prefill.budget_category_name}` : prefill.budget_category_id);
      if (prefill.division_id) divSel.el.presetSelection(prefill.division_id, prefill.division_id);
      if (prefill.branch_id) branchSel.el.presetSelection(prefill.branch_id, prefill.branch_id);
      if (prefill.zone_id) zoneSel.el.presetSelection(prefill.zone_id, prefill.zone_id);
      if (prefill.plant_id) plantSel.el.presetSelection(prefill.plant_id, prefill.plant_id);
      if (prefill.location_id) locSel.el.presetSelection(prefill.location_id, prefill.location_id);
      amountInput.value = prefill.amount_paise !== undefined && prefill.amount_paise !== null
        ? (Number(prefill.amount_paise) / 100).toFixed(2) : '';
      justInput.value = prefill.justification || '';
      customFields.fill(prefill.custom_fields);
    }

    state.lineEls.push(entry);
    linesContainer.appendChild(card);
    renumberLines();
    recomputeTotal();
    return entry;
  }

  function removeLine(key) {
    const idx = state.lineEls.findIndex((l) => l.key === key);
    if (idx === -1) return;
    state.lineEls[idx].wrap.remove();
    state.lineEls.splice(idx, 1);
    renumberLines();
    recomputeTotal();
  }

  function renumberLines() {
    state.lineEls.forEach((l, i) => { l.wrap.querySelector('.line-no').textContent = `Line ${i + 1}`; });
  }

  function clearLines() {
    for (const l of state.lineEls) l.wrap.remove();
    state.lineEls = [];
  }

  function recomputeTotal() {
    let total = 0;
    for (const l of state.lineEls) {
      try { total += parseRupeesToPaise(l.amountInput.value); } catch { /* ignored while typing */ }
    }
    document.getElementById('budgetSetupTotal').textContent = formatINR(total);
  }

  /* ---------------- header custom fields (rebuilt when defs load) ---------------- */

  let headerCustomFields = null;
  function rebuildHeaderCustomFields() {
    clear(headerCustomFieldsHost);
    headerCustomFields = buildCustomFieldsBlock(state.customFieldDefs, 'budgetHeader');
    headerCustomFieldsHost.appendChild(headerCustomFields.el);
  }

  /* ---------------- load / open ---------------- */

  function resetEditorForm() {
    clear(editorStatusHost);
    projField.el.clear(); fyField.el.clear(); periodField.el.clear();
    titleInput.value = ''; justificationInput.value = '';
    titleErr.hidden = true;
    clearLines();
    rebuildHeaderCustomFields();
    state.audit = { state: 'idle', entries: [] };
    clear(auditHost);
    recomputeTotal();
  }

  function applyDocToForm(doc) {
    resetEditorForm();
    state.doc = doc;
    editorHeadingEl.textContent = `${doc.budget_number || doc.budget_id} — ${doc.title || ''}`;
    projField.el.presetSelection(doc.project_id, `${doc.capex_code || ''} — ${doc.project_name || ''}`.replace(/^— /, ''));
    projField.el.setAttribute('disabled', '');
    fyField.el.presetSelection(doc.fiscal_year, doc.fiscal_year);
    if (doc.period_id) periodField.el.presetSelection(doc.period_id, doc.period_id);
    titleInput.value = doc.title || '';
    justificationInput.value = doc.justification || '';
    headerCustomFields.fill(doc.custom_fields);
    for (const line of doc.lines || []) addLine(line);
    const editable = EDITABLE_STATUSES.has(doc.status) && can('budget.create');
    setFormEditable(editable);
    saveDraftBtn.hidden = !editable;
    submitBtn.hidden = !editable;
    cancelDocBtn.hidden = !(can('budget.create') && !['RELEASED', 'CANCELLED', 'SUBMITTED'].includes(doc.status));
    addLineBtn.hidden = !editable;
    if (!editable) {
      editorStatusHost.appendChild(msgBox('info',
        `This budget is ${doc.status}. It is shown read-only`
        + (can('budget.create') ? '.' : '; budget.create is required to edit a draft.')));
    }
    loadAudit(doc.budget_id);
    recomputeTotal();
  }

  /** Locks every header and line control except the project selector, which
   * is ALWAYS locked once a document exists (changing the project after
   * lines have been chosen against it would invalidate every WBS
   * selection) -- see the `change` listener on projField.el above. */
  function setFormEditable(editable) {
    fyField.el.disabled = !editable;
    periodField.el.disabled = !editable;
    titleInput.disabled = !editable;
    justificationInput.disabled = !editable;
    for (const c of (headerCustomFields ? headerCustomFields.controls : [])) c.control.disabled = !editable;
    for (const l of state.lineEls) {
      const removeBtn = l.wrap.querySelector('.btn-danger');
      if (removeBtn) removeBtn.hidden = !editable;
      l.wbsSel.el.disabled = !editable;
      l.headSel.el.disabled = !editable;
      l.catSel.el.disabled = !editable;
      l.divSel.el.disabled = !editable;
      l.branchSel.el.disabled = !editable;
      l.zoneSel.el.disabled = !editable;
      l.plantSel.el.disabled = !editable;
      l.locSel.el.disabled = !editable;
      l.amountInput.disabled = !editable;
      l.justInput.disabled = !editable;
      for (const c of l.customFields.controls) c.control.disabled = !editable;
    }
  }

  function openNewDraft() {
    state.doc = null;
    editorHeadingEl.textContent = 'New budget';
    resetEditorForm();
    projField.el.removeAttribute('disabled');
    saveDraftBtn.hidden = !can('budget.create');
    submitBtn.hidden = true; // nothing to submit until a draft exists
    cancelDocBtn.hidden = true;
    addLineBtn.hidden = !can('budget.create');
    addLine();
    setFormEditable(can('budget.create')); // undo any read-only state left by a previously viewed document
    state.idemKey = uid();
    showEditor();
    titleInput.focus();
  }

  async function openExisting(budgetId) {
    showEditor();
    clear(editorStatusHost);
    editorStatusHost.appendChild(h('div', { class: 'loading' }, 'Loading budget…'));
    try {
      const doc = await getOriginal(budgetId);
      applyDocToForm(doc);
      state.idemKey = uid();
    } catch (err) {
      clear(editorStatusHost);
      const message = err instanceof BudgetApiError ? err.message : 'This budget could not be loaded.';
      editorStatusHost.appendChild(msgBox('error', h('div', {}, message), {
        alert: true,
        actions: [h('button', { type: 'button', class: 'btn-sm', onClick: () => openExisting(budgetId) }, 'Retry')],
      }));
    }
  }

  /* ---------------- validation + payload ---------------- */

  function clearAllFieldErrors() {
    projField.setError(null); fyField.setError(null); periodField.setError(null);
    titleErr.hidden = true;
    for (const l of state.lineEls) {
      l.wbsSel.setError(null); l.headSel.setError(null); l.catSel.setError(null);
      l.divSel.setError(null); l.branchSel.setError(null); l.zoneSel.setError(null);
      l.plantSel.setError(null); l.locSel.setError(null);
      l.amountErrEl.hidden = true; clear(l.amountErrEl);
      l.customFields.clearErrors();
    }
  }

  /** @returns {{payload: Object|null, focusEl: HTMLElement|null}} */
  function buildPayload() {
    clearAllFieldErrors();
    let firstInvalid = null;
    const invalid = (el, setError, message) => { setError(message); if (!firstInvalid) firstInvalid = el; };

    if (!projField.el.value) invalid(projField.el, projField.setError, 'Choose a project.');
    if (!fyField.el.value) invalid(fyField.el, fyField.setError, 'Choose a fiscal year.');
    if (!titleInput.value.trim()) {
      titleErr.hidden = false; clear(titleErr); titleErr.appendChild(text('Title is required.'));
      if (!firstInvalid) firstInvalid = titleInput;
    }

    const lines = [];
    for (const l of state.lineEls) {
      if (!l.wbsSel.el.value) invalid(l.wbsSel.el, l.wbsSel.setError, 'Choose a WBS element.');
      if (!l.headSel.el.value) invalid(l.headSel.el, l.headSel.setError, 'Choose a budget head.');
      if (!l.catSel.el.value) invalid(l.catSel.el, l.catSel.setError, 'Choose a budget category.');
      let amountPaise = null;
      try { amountPaise = parseRupeesToPaise(l.amountInput.value); } catch (err) {
        l.amountErrEl.hidden = false; clear(l.amountErrEl); l.amountErrEl.appendChild(text(err.message));
        if (!firstInvalid) firstInvalid = l.amountInput;
      }
      lines.push({
        wbs_id: l.wbsSel.el.value,
        budget_head_id: l.headSel.el.value,
        budget_category_id: l.catSel.el.value,
        amount_paise: amountPaise,
        justification: l.justInput.value.trim() || undefined,
        division_id: l.divSel.el.value || undefined,
        branch_id: l.branchSel.el.value || undefined,
        zone_id: l.zoneSel.el.value || undefined,
        plant_id: l.plantSel.el.value || undefined,
        location_id: l.locSel.el.value || undefined,
        custom_fields: l.customFields.values(),
      });
    }

    if (firstInvalid) return { payload: null, focusEl: firstInvalid };

    return {
      payload: {
        project_id: projField.el.value,
        fiscal_year: fyField.el.value,
        title: titleInput.value.trim(),
        period_id: periodField.el.value || undefined,
        justification: justificationInput.value.trim() || undefined,
        custom_fields: headerCustomFields.values(),
        lines,
      },
      focusEl: null,
    };
  }

  /** Map a 422 BUDGET_LINES_INVALID `problems[]` entry back onto its line editor. */
  function applyLineProblems(problems) {
    for (const p of problems || []) {
      const idx = typeof p.line === 'number' ? p.line - 1 : -1;
      const line = state.lineEls[idx];
      if (!line) continue;
      const field = String(p.field || '');
      const message = p.message || 'Invalid value.';
      if (field === 'wbs_id') line.wbsSel.setError(message);
      else if (field === 'budget_head_id') line.headSel.setError(message);
      else if (field === 'budget_category_id') line.catSel.setError(message);
      else if (field === 'amount_paise' || field === 'amount_rupees') {
        line.amountErrEl.hidden = false; clear(line.amountErrEl); line.amountErrEl.appendChild(text(message));
      } else if (field.startsWith('custom_fields.')) {
        line.customFields.applyProblems([p], 'custom_fields');
      }
    }
  }

  function setDocBusy(busy, label) {
    state.docBusy = busy; state.docBusyLabel = label || '';
    saveDraftBtn.disabled = busy; submitBtn.disabled = busy; cancelDocBtn.disabled = busy; addLineBtn.disabled = busy;
  }

  /* ---------------- save / submit / cancel ---------------- */

  async function saveDraft() {
    const { payload, focusEl } = buildPayload();
    if (!payload) { if (focusEl) focusEl.focus(); announce('Fix the highlighted fields before saving.'); return; }
    clear(editorStatusHost);
    setDocBusy(true, 'Saving…');
    saveDraftBtn.textContent = 'Saving…';
    try {
      let doc;
      if (state.doc) {
        doc = await updateOriginal(state.doc.budget_id, { ...payload, expected_version: state.doc.version_no });
      } else {
        doc = await createOriginal(payload, state.idemKey);
        state.idemKey = uid(); // regenerated after a successful create, per the idempotency contract
      }
      applyDocToForm(doc);
      loadList(true);
      announce(`${doc.budget_number || doc.budget_id} saved as draft.`);
      editorStatusHost.appendChild(msgBox('success', 'Draft saved.'));
    } catch (err) {
      handleDocError(err, 'save');
    } finally {
      setDocBusy(false);
      saveDraftBtn.textContent = 'Save draft';
    }
  }

  async function doSubmit() {
    if (!state.doc) { await saveDraft(); if (!state.doc) return; }
    const { payload, focusEl } = buildPayload();
    if (!payload) { if (focusEl) focusEl.focus(); announce('Fix the highlighted fields before submitting.'); return; }
    clear(editorStatusHost);
    setDocBusy(true, 'Saving before submit…');
    try {
      // Save whatever is currently in the editor first, so what gets submitted
      // is exactly what is on screen.
      const saved = await updateOriginal(state.doc.budget_id, { ...payload, expected_version: state.doc.version_no });
      applyDocToForm(saved);
      submitBtn.textContent = 'Submitting…';
      const result = await submitOriginal(saved.budget_id, saved.version_no);
      announce(`${saved.budget_number || saved.budget_id} submitted for approval.`);
      editorStatusHost.appendChild(msgBox('success', h('div', {}, [
        h('div', {}, `Submitted. Approval instance ${result.approval_instance_id || '—'}.`),
        h('div', { class: 'btn-row' }, [
          h('a', { class: 'btn-sm linkish', href: `?instance=${encodeURIComponent(result.approval_instance_id || '')}#approval-request` }, 'Open approval request'),
          h('button', { type: 'button', class: 'btn-sm', dataset: { nav: 'approval-inbox' } }, 'Go to My Approval Inbox'),
        ]),
      ])));
      const fresh = await getOriginal(saved.budget_id);
      applyDocToForm(fresh);
      loadList(true);
    } catch (err) {
      handleDocError(err, 'submit');
    } finally {
      setDocBusy(false);
      submitBtn.textContent = 'Submit for approval';
    }
  }

  function handleDocError(err, action) {
    clearAllFieldErrors();
    if (err instanceof BudgetApiError && err.kind === 'conflict' && err.status === 409 && err.code !== undefined
      && (err.code === 'VERSION_CONFLICT' || err.code === 'BUDGET_STALE')) {
      editorStatusHost.appendChild(msgBox('warning', h('div', {}, [
        h('div', {}, 'Someone else changed this budget since it was loaded.'),
        h('div', { class: 'btn-row' }, [
          h('button', {
            type: 'button', class: 'btn-sm',
            onClick: async () => { if (state.doc) await openExisting(state.doc.budget_id); },
          }, 'Reload latest values'),
        ]),
      ])));
      return;
    }
    if (err instanceof BudgetApiError) {
      const { message, problems } = problemDetail(err);
      if (problems.length) applyLineProblems(problems);
      editorStatusHost.appendChild(msgBox('error', h('div', {}, message), { messageId: err.messageId, alert: true }));
      return;
    }
    editorStatusHost.appendChild(msgBox('error', h('div', {}, `This budget could not be ${action === 'submit' ? 'submitted' : 'saved'}. Try again.`), { alert: true }));
  }

  function openCancelDialog() {
    if (!state.doc) return;
    cancelReasonDialog.open({
      title: `Cancel ${state.doc.budget_number || state.doc.budget_id}`,
      describe: 'This budget will be marked cancelled. It is not deleted and remains visible in its history, but cannot be edited or submitted again.',
      confirmLabel: 'Cancel budget',
      danger: true,
      opener: cancelDocBtn,
      onConfirm: (reason) => cancelOriginal(state.doc.budget_id, reason),
      onSuccess: async () => {
        announce('Budget cancelled.');
        if (state.doc) await openExisting(state.doc.budget_id);
        loadList(true);
      },
    });
  }

  /* ---------------- audit history ---------------- */

  async function loadAudit(budgetId) {
    state.audit = { state: 'loading', entries: [] };
    renderAudit();
    try {
      const data = await getOriginalAudit(budgetId);
      state.audit = { state: 'ready', entries: (data && data.entries) || [] };
    } catch {
      state.audit = { state: 'error', entries: [] };
    }
    renderAudit();
  }

  function renderAudit() {
    clear(auditHost);
    if (state.audit.state === 'idle') { auditHost.appendChild(h('div', { class: 'muted small' }, 'Save this budget to see its audit history.')); return; }
    if (state.audit.state === 'loading') { auditHost.appendChild(h('div', { class: 'loading' }, 'Loading audit history…')); return; }
    if (state.audit.state === 'error') { auditHost.appendChild(h('div', { class: 'muted small' }, 'Audit history could not be loaded.')); return; }
    if (!state.audit.entries.length) { auditHost.appendChild(h('div', { class: 'muted small' }, 'No audit entries yet.')); return; }
    auditHost.appendChild(h('ul', { class: 'budget-audit-list' }, state.audit.entries.map((e) => h('li', {}, [
      h('span', { class: 'seq' }, `#${e.seq ?? '—'}`),
      text(`${e.action || '—'} · ${e.actor || '—'} · ${formatAuditTimestamp(e.at)}`),
      e.detail ? h('div', { class: 'muted small' }, e.detail) : null,
    ].filter(Boolean)))));
  }

  /* ================================================================
     UPLOAD BUDGET (CSV import)
     ================================================================ */

  const importProjField = govField('Project', 'project', { required: true });
  const importFyField = govField('Fiscal year', 'fiscal_year', { required: true });
  const importTitleInput = h('input', { type: 'text', id: 'budgetImportTitle', required: true });
  const downloadTemplateBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => downloadTemplate() }, 'Download CSV template');
  const csvTextarea = h('textarea', { id: 'budgetImportCsv', rows: '10', placeholder: 'Paste CSV here, or choose a file below.' });
  const csvFileInput = h('input', { type: 'file', accept: '.csv,text/csv', onChange: (ev) => loadCsvFile(ev.target.files && ev.target.files[0]) });
  const previewBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => runPreview() }, 'Preview');
  const importBtn = h('button', { type: 'button', class: 'btn-primary btn-sm', hidden: true, onClick: () => runImport() }, 'Import');
  const previewHost = h('div', { class: 'budget-import-problems' });
  const importStatusHost = h('div', { id: 'budgetImportStatus' });

  const uploadHeading = h('h2', { class: 'section-title' }, 'Upload budget');
  panelUpload.appendChild(uploadHeading);
  panelUpload.appendChild(h('div', { class: 'budget-import-panel' }, [
    h('div', { class: 'budget-doc-header' }, [
      importProjField.wrap,
      importFyField.wrap,
      h('div', { class: 'field span-2' }, [h('label', { for: 'budgetImportTitle' }, 'Title *'), importTitleInput]),
    ]),
    h('div', { class: 'budget-import-actions' }, [downloadTemplateBtn]),
    h('div', { class: 'field' }, [h('label', { for: 'budgetImportCsv' }, 'CSV content'), csvTextarea]),
    h('div', { class: 'field' }, [h('label', {}, 'Or choose a CSV file'), csvFileInput]),
    importStatusHost,
    h('div', { class: 'budget-import-actions' }, [previewBtn, importBtn]),
    previewHost,
  ]));

  function loadCsvFile(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => { csvTextarea.value = String(reader.result || ''); };
    reader.readAsText(file);
  }

  async function downloadTemplate() {
    clear(importStatusHost);
    try {
      const csv = await getImportTemplate();
      const blob = new Blob([csv], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = h('a', { href: url, download: 'original-budget-template.csv' });
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      importStatusHost.appendChild(msgBox('error', h('div', {}, err instanceof BudgetApiError ? err.message : 'The template could not be downloaded.'), { alert: true }));
    }
  }

  async function runPreview() {
    clear(importStatusHost);
    clear(previewHost);
    importBtn.hidden = true;
    if (!importProjField.el.value) { importProjField.setError('Choose a project.'); return; }
    importProjField.setError(null);
    const csvText = csvTextarea.value;
    if (!csvText.trim()) { importStatusHost.appendChild(msgBox('error', 'Provide CSV content to preview.', { alert: true })); return; }
    previewBtn.disabled = true; previewBtn.textContent = 'Checking…';
    try {
      const result = await previewImport({ projectId: importProjField.el.value, csvText });
      renderPreview(result);
      importBtn.hidden = !result.valid;
    } catch (err) {
      const message = err instanceof BudgetApiError ? problemDetail(err).message : 'The CSV could not be checked.';
      importStatusHost.appendChild(msgBox('error', h('div', {}, message), { alert: true }));
    } finally {
      previewBtn.disabled = false; previewBtn.textContent = 'Preview';
    }
  }

  function renderPreview(result) {
    clear(previewHost);
    const problems = (result && result.problems) || [];
    if (!problems.length) {
      previewHost.appendChild(msgBox('success', h('div', {}, [
        h('div', {}, `${result.rows ?? result.lines?.length ?? 0} row(s) valid.`),
        h('div', {}, `Total: ${formatINR(result.total_paise)}`),
      ])));
      return;
    }
    const table = createDataTable({
      caption: 'CSV validation problems',
      emptyMessage: 'No problems.',
      columns: [
        { key: 'row', label: 'Row', numeric: true, render: (r) => text(String(r.row ?? '—')) },
        { key: 'column', label: 'Column', render: (r) => text(r.column || '—') },
        { key: 'code', label: 'Code', render: (r) => h('span', { class: 'mono xs' }, r.code || '—') },
        { key: 'message', label: 'Message', render: (r) => text(r.message || '—') },
      ],
    });
    table.renderRows(problems);
    previewHost.appendChild(msgBox('error', 'This file cannot be imported until every problem below is fixed.', { alert: true }));
    previewHost.appendChild(table.el);
  }

  async function runImport() {
    clear(importStatusHost);
    let firstInvalid = null;
    importProjField.setError(null); importFyField.setError(null);
    if (!importProjField.el.value) { importProjField.setError('Choose a project.'); firstInvalid = importProjField.el; }
    if (!importFyField.el.value) { importFyField.setError('Choose a fiscal year.'); firstInvalid = firstInvalid || importFyField.el; }
    if (!importTitleInput.value.trim()) { firstInvalid = firstInvalid || importTitleInput; }
    const csvText = csvTextarea.value;
    if (!csvText.trim()) firstInvalid = firstInvalid || csvTextarea;
    if (firstInvalid) { firstInvalid.focus(); announce('Fix the highlighted fields before importing.'); return; }

    importBtn.disabled = true; importBtn.textContent = 'Importing…';
    try {
      const doc = await commitImport({
        projectId: importProjField.el.value,
        fiscalYear: importFyField.el.value,
        title: importTitleInput.value.trim(),
        csvText,
      }, state.importIdemKey);
      state.importIdemKey = uid();
      importStatusHost.appendChild(msgBox('success', `${doc.budget_number || doc.budget_id} created as a draft.`));
      csvTextarea.value = '';
      importTitleInput.value = '';
      clear(previewHost);
      importBtn.hidden = true;
      selectTab('budgets');
      await openExisting(doc.budget_id);
      loadList(true);
    } catch (err) {
      const { message, problems } = err instanceof BudgetApiError ? problemDetail(err) : { message: 'The import could not be completed.', problems: [] };
      importStatusHost.appendChild(msgBox('error', h('div', {}, message), { alert: true }));
      if (problems.length) renderPreview({ problems });
    } finally {
      importBtn.disabled = false; importBtn.textContent = 'Import';
    }
  }

  /* ================================================================
     boot
     ================================================================ */

  (async function boot() {
    const principal = await getPrincipal();
    state.permissions = principal.permissions;
    newBudgetBtn.hidden = !can('budget.create');

    try {
      const cf = await listCustomFields();
      state.customFieldDefs = (cf && cf.items) || [];
    } catch {
      state.customFieldDefs = [];
    }
    rebuildHeaderCustomFields();

    loadList(true);
  }());
}
