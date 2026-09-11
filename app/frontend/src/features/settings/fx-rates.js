/* app/frontend/src/features/settings/fx-rates.js
   SCR "Exchange Rates" -- the rate book behind every foreign-currency
   translation (Fable 5.1, migration 028), read and maintained through
   app/backend/api/fx_admin.py and its pg/fx_admin.py service. Modelled on
   features/settings/budget-categories.js's list / create / confirm shape,
   with the client narrowed to this feature's own fx-api.js.

   WHAT THE SCREEN NEVER DOES
   * It never turns a rate into a Number. The rate is an exact decimal
     string from the server and is shown as given; the create form's rate
     field is a text input and the server validates it (fx.parse_rate).
   * It never edits a rate. A rate is created, activated, retired or
     superseded by a correction -- the numeric value on a row is immutable
     at the database, and the screen offers no control that pretends
     otherwise.
   * It never does float arithmetic on money. The "Test lookup" preview
     shows the integer paise the server computed, formatted by string
     slicing, and the amount it sends is an integer in the source
     currency's own minor units.

   Write controls (New rate, Import, Activate, Retire) render only when this
   session holds fx.manage; that gate is presentational, the server
   enforces it on every write route (and activation is additionally
   refused for the rate's own creator: FX_SELF_ACTIVATION).
*/

import { h, text, clear } from '../../core/dom.js';
import {
  listRates, createRate, activateRate, deactivateRate, lookupRate, getHistory,
  previewImport, commitImport, FxApiError,
} from './fx-api.js';
import { getPrincipal } from '../budget/budget-api.js';
import { formatSettingsDate } from './format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';

const STATUS_TONE = { AWAITING_ACTIVATION: 'warning', ACTIVE: 'positive', RETIRED: 'neutral', SUPERSEDED: 'neutral' };
const STATUS_LABEL = { AWAITING_ACTIVATION: 'Awaiting activation', ACTIVE: 'Active', RETIRED: 'Retired', SUPERSEDED: 'Superseded' };
const IMPORT_COLUMNS = ['from_currency', 'to_currency', 'rate_date', 'rate', 'rate_source', 'source_reference', 'note'];

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

function statusOf(row) {
  const key = row && row.status;
  return statusChip({ label: STATUS_LABEL[key] || key || '—', tone: STATUS_TONE[key] || 'neutral' });
}

/** Same envelope-unwrap reasoning as budget-categories.js::problemDetail. */
function problemDetail(err) {
  const raw = err && err.body;
  const nested = raw && typeof raw === 'object' && raw.detail && typeof raw.detail === 'object' ? raw.detail : null;
  const message = (nested && typeof nested.detail === 'string' && nested.detail.trim()) ? nested.detail : ((err && err.message) || 'This request could not be completed.');
  const code = (nested && nested.code) || (err && err.code) || null;
  const problems = nested && Array.isArray(nested.problems) ? nested.problems : null;
  return { message, code, problems };
}

/** Integer paise -> "1,23,456.78" by string slicing. No division: paise are
 * integers and a rupee is not a quantity to approximate. */
function formatPaise(value) {
  if (value === null || value === undefined) return '—';
  const negative = String(value).startsWith('-');
  const digits = String(value).replace(/^-/, '').replace(/\D/g, '').padStart(3, '0');
  const whole = digits.slice(0, -2);
  const fraction = digits.slice(-2);
  const last3 = whole.slice(-3);
  const rest = whole.slice(0, -3);
  const grouped = rest ? `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ',')},${last3}` : last3;
  return `${negative ? '-' : ''}₹${grouped}.${fraction}`;
}

function newIdempotencyKey() {
  try { return crypto.randomUUID(); } catch { return `k-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
}

/** Paste box -> rows. Accepts a JSON array of objects, or CSV whose first
 * line is a header naming any of IMPORT_COLUMNS. Quoted fields are not
 * supported; a source_reference with a comma belongs in the JSON form. */
function parseImportText(raw) {
  const textValue = String(raw || '').trim();
  if (!textValue) return { rows: [], error: 'Paste JSON rows or CSV lines first.' };
  if (textValue.startsWith('[')) {
    try {
      const parsed = JSON.parse(textValue);
      if (!Array.isArray(parsed)) return { rows: [], error: 'JSON must be an array of rate objects.' };
      return { rows: parsed, error: null };
    } catch {
      return { rows: [], error: 'The JSON could not be parsed.' };
    }
  }
  const lines = textValue.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const header = lines[0].split(',').map((c) => c.trim().toLowerCase());
  const unknown = header.filter((c) => !IMPORT_COLUMNS.includes(c));
  if (unknown.length) return { rows: [], error: `Unknown CSV column(s): ${unknown.join(', ')}. Use ${IMPORT_COLUMNS.join(', ')}.` };
  const rows = lines.slice(1).map((line) => {
    const cells = line.split(',').map((c) => c.trim());
    const row = {};
    header.forEach((col, i) => { if (cells[i] !== undefined && cells[i] !== '') row[col] = cells[i]; });
    return row;
  });
  return { rows, error: rows.length ? null : 'The CSV has a header and no rows.' };
}

export function mountFxRates(root) {
  if (!root) return;
  const liveRegion = document.getElementById('fxRatesLiveRegion') || document.getElementById('budgetLiveRegion');
  function announce(message) { if (liveRegion) liveRegion.textContent = message; }

  const state = {
    permissions: new Set(),
    filters: { source: '', target: '', status: '', effectiveFrom: '', effectiveTo: '' },
    items: [],
    nextCursor: null,
    listState: 'loading', // loading|empty|error|permission|ready
    listError: null,
  };
  function can(permission) { return state.permissions.has(permission); }

  /* ---------------- toolbar ---------------- */

  const sourceInput = h('input', { type: 'text', id: 'fxFilterSource', maxlength: '3', placeholder: 'USD', autocomplete: 'off' });
  const targetInput = h('input', { type: 'text', id: 'fxFilterTarget', maxlength: '3', placeholder: 'INR', autocomplete: 'off' });
  const statusSelect = h('select', { id: 'fxFilterStatus' }, [
    h('option', { value: '' }, 'All statuses'),
    ...Object.keys(STATUS_LABEL).map((k) => h('option', { value: k }, STATUS_LABEL[k])),
  ]);
  const fromInput = h('input', { type: 'date', id: 'fxFilterFrom' });
  const toInput = h('input', { type: 'date', id: 'fxFilterTo' });
  const newBtn = h('button', { type: 'button', class: 'btn-primary btn-sm', hidden: true, onClick: (ev) => openCreate(ev.currentTarget) }, '+ New rate');
  const importBtn = h('button', { type: 'button', class: 'btn-sm', hidden: true, onClick: (ev) => openImport(ev.currentTarget) }, 'Import…');

  const toolbar = h('form', {
    class: 'toolbar fx-toolbar',
    onSubmit: (ev) => { ev.preventDefault(); readFilters(); loadList(); },
  }, [
    h('div', { class: 'field' }, [h('label', { for: 'fxFilterSource' }, 'Source'), sourceInput]),
    h('div', { class: 'field' }, [h('label', { for: 'fxFilterTarget' }, 'Target'), targetInput]),
    h('div', { class: 'field' }, [h('label', { for: 'fxFilterStatus' }, 'Status'), statusSelect]),
    h('div', { class: 'field' }, [h('label', { for: 'fxFilterFrom' }, 'Effective from'), fromInput]),
    h('div', { class: 'field' }, [h('label', { for: 'fxFilterTo' }, 'Effective to'), toInput]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), h('button', { type: 'submit', class: 'btn-sm' }, 'Apply')]),
    h('div', { class: 'field field-action grow-action fx-toolbar-writes' }, [h('span', { class: 'sr-only' }, ''), importBtn, newBtn]),
  ]);

  function readFilters() {
    state.filters = {
      source: sourceInput.value.trim().toUpperCase(),
      target: targetInput.value.trim().toUpperCase(),
      status: statusSelect.value,
      effectiveFrom: fromInput.value,
      effectiveTo: toInput.value,
    };
  }

  const statusHost = h('div', { id: 'fxRatesStatus' });
  const moreBtn = h('button', { type: 'button', class: 'btn-sm', hidden: true, onClick: () => loadMore() }, 'Load more');
  const moreHost = h('div', { class: 'fx-more' }, [moreBtn]);

  const table = createDataTable({
    caption: 'Exchange rates — pair, rate, source, effective date, status, created and activated by',
    emptyMessage: 'No exchange rates match these filters.',
    columns: [
      { key: 'pair', label: 'Pair', render: (row) => h('span', { class: 'mono' }, `${row.from_currency}/${row.to_currency}`) },
      { key: 'rate', label: 'Rate', numeric: true, render: (row) => h('span', { class: 'mono fx-rate' }, row.rate || '—') },
      { key: 'rate_source', label: 'Source', render: (row) => text(row.rate_source || '—') },
      { key: 'rate_date', label: 'Effective', render: (row) => text(formatSettingsDate(row.rate_date)) },
      { key: 'status', label: 'Status', render: (row) => statusOf(row) },
      { key: 'created_by', label: 'Created by', render: (row) => text(row.created_by || '—') },
      { key: 'activated_by', label: 'Activated by', render: (row) => text(row.activated_by || '—') },
      {
        key: 'actions',
        label: 'Actions',
        render: (row) => h('div', { class: 'row-actions' }, [
          h('button', { type: 'button', class: 'btn-sm', onClick: (ev) => openHistory(row, ev.currentTarget) }, 'History'),
          can('fx.manage') && row.status === 'AWAITING_ACTIVATION'
            ? h('button', { type: 'button', class: 'btn-sm btn-primary', onClick: (ev) => openActivate(row, ev.currentTarget) }, 'Activate')
            : null,
          can('fx.manage') && row.status === 'ACTIVE'
            ? h('button', { type: 'button', class: 'btn-sm btn-danger', onClick: (ev) => openRetire(row, ev.currentTarget) }, 'Retire')
            : null,
        ].filter(Boolean)),
      },
    ],
  });

  root.appendChild(h('h2', { class: 'sr-only' }, 'Exchange rates'));
  root.appendChild(toolbar);
  root.appendChild(statusHost);
  root.appendChild(table.el);
  root.appendChild(moreHost);

  function renderList() {
    clear(statusHost);
    moreBtn.hidden = !(state.listState === 'ready' && state.nextCursor);
    if (state.listState === 'loading') {
      statusHost.hidden = true; table.el.hidden = false; table.renderSkeleton(); return;
    }
    if (state.listState === 'permission') {
      statusHost.hidden = false; table.el.hidden = true; table.renderRows([]);
      statusHost.appendChild(h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '⇄'),
        h('div', {}, state.listError ? state.listError.message : 'No exchange rates were found.'),
      ]));
      return;
    }
    if (state.listState === 'error') {
      statusHost.hidden = false; table.el.hidden = true; table.renderRows([]);
      const err = state.listError;
      statusHost.appendChild(msgBox('error', h('div', {}, err ? err.message : 'Exchange rates could not be loaded.'), {
        messageId: err && err.messageId, alert: true,
        actions: [h('button', { type: 'button', class: 'btn-sm', onClick: () => loadList() }, 'Retry')],
      }));
      return;
    }
    if (state.listState === 'empty') {
      statusHost.hidden = false; table.el.hidden = true; table.renderRows([]);
      statusHost.appendChild(h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '⇄'),
        h('div', {}, can('fx.manage')
          ? 'No exchange rates match these filters. Use "New rate" or "Import…" above to record some.'
          : 'No exchange rates match these filters.'),
      ]));
      return;
    }
    statusHost.hidden = true; table.el.hidden = false; table.renderRows(state.items);
  }

  function listParams(cursor) {
    const f = state.filters;
    return { source: f.source, target: f.target, status: f.status, effectiveFrom: f.effectiveFrom, effectiveTo: f.effectiveTo, cursor, limit: 50 };
  }

  async function loadList() {
    state.listState = 'loading'; state.listError = null; state.nextCursor = null; renderList();
    try {
      const data = await listRates(listParams(null));
      state.items = (data && data.items) || [];
      state.nextCursor = (data && data.next_cursor) || null;
      state.listState = state.items.length ? 'ready' : 'empty';
      announce(state.listState === 'ready' ? `${state.items.length} exchange rates loaded.` : 'No exchange rates match these filters.');
    } catch (err) {
      if (err instanceof FxApiError && err.kind === 'notfound') {
        state.listState = 'permission';
        state.listError = { message: err.message, messageId: err.messageId };
      } else {
        state.listState = 'error';
        state.listError = { message: err instanceof FxApiError ? err.message : 'Exchange rates could not be loaded. Try again.', messageId: err instanceof FxApiError ? err.messageId : null };
      }
      announce('Exchange rates could not be loaded.');
    }
    renderList();
  }

  async function loadMore() {
    if (!state.nextCursor) return;
    moreBtn.disabled = true; moreBtn.textContent = 'Loading…';
    try {
      const data = await listRates(listParams(state.nextCursor));
      state.items = state.items.concat((data && data.items) || []);
      state.nextCursor = (data && data.next_cursor) || null;
      renderList();
      announce(`${state.items.length} exchange rates loaded.`);
    } catch (err) {
      announce(err instanceof FxApiError ? err.message : 'More rates could not be loaded.');
    } finally {
      moreBtn.disabled = false; moreBtn.textContent = 'Load more';
    }
  }

  /* ---------------- test lookup ---------------- */

  const luSource = h('input', { type: 'text', id: 'fxLookupSource', maxlength: '3', placeholder: 'USD', required: true, autocomplete: 'off' });
  const luTarget = h('input', { type: 'text', id: 'fxLookupTarget', maxlength: '3', placeholder: 'INR', autocomplete: 'off' });
  const luDate = h('input', { type: 'date', id: 'fxLookupDate', required: true });
  const luAmount = h('input', { type: 'text', inputmode: 'numeric', id: 'fxLookupAmount', placeholder: 'e.g. 100000' });
  const luResult = h('div', { class: 'fx-lookup-result', 'aria-live': 'polite' });
  const luBtn = h('button', { type: 'submit', class: 'btn-sm btn-primary' }, 'Look up');
  const lookupForm = h('form', { class: 'toolbar fx-lookup', novalidate: true, onSubmit: (ev) => { ev.preventDefault(); runLookup(); } }, [
    h('div', { class: 'field' }, [h('label', { for: 'fxLookupSource' }, 'Source *'), luSource]),
    h('div', { class: 'field' }, [h('label', { for: 'fxLookupTarget' }, 'Target'), luTarget]),
    h('div', { class: 'field' }, [h('label', { for: 'fxLookupDate' }, 'Date *'), luDate]),
    h('div', { class: 'field' }, [h('label', { for: 'fxLookupAmount' }, 'Amount (minor units)'), luAmount]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), luBtn]),
  ]);
  root.appendChild(h('section', { class: 'fx-panel', 'aria-labelledby': 'fxLookupTitle' }, [
    h('h3', { id: 'fxLookupTitle', class: 'section-title' }, 'Test lookup'),
    h('p', { class: 'fx-hint' }, 'Answers exactly what the bills ingestion will get for this pair and date: the ACTIVE rate, or the coded refusal when none covers it. No fallback to another day, a retired rate, or 1.'),
    lookupForm,
    luResult,
  ]));

  async function runLookup() {
    clear(luResult);
    const source = luSource.value.trim().toUpperCase();
    const date = luDate.value;
    if (!source || !date) {
      luResult.appendChild(msgBox('warning', h('div', {}, 'Source currency and date are required.')));
      (source ? luDate : luSource).focus();
      return;
    }
    const amountRaw = luAmount.value.trim();
    if (amountRaw && !/^\d+$/.test(amountRaw)) {
      luResult.appendChild(msgBox('warning', h('div', {}, 'Amount must be a whole number of minor units (paise, fils, yen).')));
      luAmount.focus();
      return;
    }
    luBtn.disabled = true; luBtn.textContent = 'Looking up…';
    try {
      const res = await lookupRate({ source, target: luTarget.value.trim().toUpperCase() || undefined, date, amountMinor: amountRaw || undefined });
      const lines = [
        h('div', {}, [h('strong', {}, `${res.source}/${res.target} on ${formatSettingsDate(res.date)}: `), h('span', { class: 'mono fx-rate' }, res.rate)]),
        h('div', { class: 'xs' }, res.identity ? 'Identity translation (same currency).' : `Source ${res.rate_source} · ${res.fx_rate_id}`),
        h('div', { class: 'xs' }, `Minor-unit exponent: ${res.source} ${res.source_minor_exponent} · ${res.target} ${res.target_minor_exponent}`),
      ];
      if (res.translated_paise !== undefined) {
        lines.push(h('div', {}, [h('strong', {}, `${res.amount_minor} minor units of ${res.source} → `), h('span', { class: 'mono' }, `${formatPaise(res.translated_paise)} (${res.translated_paise} paise)`)]));
      }
      luResult.appendChild(msgBox('success', h('div', {}, lines)));
      announce(`Rate found: ${res.rate}.`);
    } catch (err) {
      const detail = err instanceof FxApiError ? problemDetail(err) : { message: 'The lookup could not be completed.', code: null };
      luResult.appendChild(msgBox('error', h('div', {}, [
        detail.code ? h('div', {}, [h('span', { class: 'mono fx-code' }, detail.code)]) : null,
        h('div', {}, detail.message),
      ].filter(Boolean)), { alert: true, messageId: err instanceof FxApiError ? err.messageId : null }));
      announce(detail.code ? `Refused: ${detail.code}.` : 'The lookup could not be completed.');
    } finally {
      luBtn.disabled = false; luBtn.textContent = 'Look up';
    }
  }

  /* ---------------- history dialog ---------------- */

  const histTitle = h('h2', { id: 'fxHistoryTitle' }, '');
  const histBody = h('div', { class: 'dlg-body fx-history-body' });
  const histClose = h('button', { type: 'button', class: 'dlg-close', 'aria-label': 'Close dialog', onClick: () => histDialog.close() }, '✕');
  const histDialog = h('dialog', { 'aria-labelledby': 'fxHistoryTitle', class: 'fx-dialog-wide' }, [
    h('div', { class: 'dlg-head' }, [histTitle, histClose]),
    histBody,
    h('div', { class: 'dlg-foot' }, [h('button', { type: 'button', class: 'btn-sm', onClick: () => histDialog.close() }, 'Close')]),
  ]);
  let histOpener = null;
  histDialog.addEventListener('close', () => { if (histOpener && typeof histOpener.focus === 'function') histOpener.focus(); histOpener = null; });

  const histTable = createDataTable({
    caption: 'Every rate ever recorded for this pair, newest first, in every state',
    emptyMessage: 'No rates recorded for this pair.',
    columns: [
      { key: 'rate_date', label: 'Effective', render: (row) => text(formatSettingsDate(row.rate_date)) },
      { key: 'rate', label: 'Rate', numeric: true, render: (row) => h('span', { class: 'mono fx-rate' }, row.rate) },
      { key: 'rate_source', label: 'Source', render: (row) => text(row.rate_source || '—') },
      { key: 'status', label: 'Status', render: (row) => statusOf(row) },
      { key: 'created_by', label: 'Created', render: (row) => text(`${row.created_by || '—'} · ${formatSettingsDate(row.captured_at)}`) },
      { key: 'activated_by', label: 'Activated', render: (row) => text(row.activated_by ? `${row.activated_by} · ${formatSettingsDate(row.activated_at)}` : '—') },
      {
        key: 'lifecycle',
        label: 'Retired / superseded',
        render: (row) => text(row.superseded_by
          ? `Superseded by ${row.superseded_by}`
          : (row.deactivated_by ? `${row.deactivated_by}: ${row.deactivation_reason || ''}` : '—')),
      },
      { key: 'fx_rate_id', label: 'Id', render: (row) => h('span', { class: 'mono' }, row.fx_rate_id) },
    ],
  });

  async function openHistory(row, opener) {
    histOpener = opener || document.activeElement;
    histTitle.textContent = `History — ${row.from_currency}/${row.to_currency}`;
    clear(histBody);
    histBody.appendChild(histTable.el);
    histTable.renderSkeleton();
    if (!histDialog.isConnected) document.body.appendChild(histDialog);
    histDialog.showModal();
    try {
      const data = await getHistory({ source: row.from_currency, target: row.to_currency });
      histTable.renderRows((data && data.items) || []);
    } catch (err) {
      clear(histBody);
      histBody.appendChild(msgBox('error', h('div', {}, err instanceof FxApiError ? problemDetail(err).message : 'History could not be loaded.'), { alert: true }));
    }
  }

  /* ---------------- create dialog ---------------- */

  const cTitle = h('h2', { id: 'fxCreateTitle' }, 'New exchange rate');
  const cClose = h('button', { type: 'button', class: 'dlg-close', 'aria-label': 'Close dialog', onClick: () => createDialog.close() }, '✕');
  const cError = h('div', { class: 'msg msg-error', role: 'alert', hidden: true });
  const cFrom = h('input', { type: 'text', id: 'fxCreateFrom', maxlength: '3', required: true, autocomplete: 'off' });
  const cTo = h('input', { type: 'text', id: 'fxCreateTo', maxlength: '3', value: 'INR', autocomplete: 'off' });
  const cDate = h('input', { type: 'date', id: 'fxCreateDate', required: true });
  const cRate = h('input', { type: 'text', inputmode: 'decimal', id: 'fxCreateRate', required: true, placeholder: 'e.g. 83.7200', autocomplete: 'off' });
  const cSource = h('input', { type: 'text', id: 'fxCreateSource', required: true, placeholder: 'RBI_REFERENCE', maxlength: '80' });
  const cRef = h('input', { type: 'text', id: 'fxCreateRef', placeholder: 'e.g. RBI reference rate bulletin' });
  const cNote = h('textarea', { id: 'fxCreateNote', rows: '2' });
  const cFieldErr = {};
  function fieldErr(id) { cFieldErr[id] = h('div', { class: 'field-err xs st-negative', hidden: true }); return cFieldErr[id]; }
  const cSubmit = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Record (pending activation)');
  const createForm = h('form', { novalidate: true }, [
    h('div', { class: 'dlg-msg' }, [cError]),
    h('div', { class: 'dlg-body' }, [
      h('p', { class: 'fx-hint' }, 'A recorded rate awaits activation by a different user before it translates anything. The rate is an exact decimal with up to eight places; it is validated by the server and never rounded.'),
      h('div', { class: 'fx-grid-2' }, [
        h('div', { class: 'field' }, [h('label', { for: 'fxCreateFrom' }, 'Source currency *'), cFrom, fieldErr('from')]),
        h('div', { class: 'field' }, [h('label', { for: 'fxCreateTo' }, 'Target currency'), cTo]),
      ]),
      h('div', { class: 'fx-grid-2' }, [
        h('div', { class: 'field' }, [h('label', { for: 'fxCreateDate' }, 'Effective date *'), cDate, fieldErr('date')]),
        h('div', { class: 'field' }, [h('label', { for: 'fxCreateRate' }, 'Rate (decimal) *'), cRate, fieldErr('rate')]),
      ]),
      h('div', { class: 'field' }, [h('label', { for: 'fxCreateSource' }, 'Source *'), cSource, fieldErr('source')]),
      h('div', { class: 'field' }, [h('label', { for: 'fxCreateRef' }, 'Source reference'), cRef]),
      h('div', { class: 'field' }, [h('label', { for: 'fxCreateNote' }, 'Note'), cNote]),
    ]),
    h('div', { class: 'dlg-foot' }, [
      h('button', { type: 'button', class: 'btn-sm', onClick: () => createDialog.close() }, 'Cancel'),
      cSubmit,
    ]),
  ]);
  const createDialog = h('dialog', { 'aria-labelledby': 'fxCreateTitle' }, [h('div', { class: 'dlg-head' }, [cTitle, cClose]), createForm]);
  let createOpener = null;
  createDialog.addEventListener('close', () => { if (createOpener && typeof createOpener.focus === 'function') createOpener.focus(); createOpener = null; });

  function clearCreateErrors() {
    cError.hidden = true; clear(cError);
    Object.values(cFieldErr).forEach((el) => { el.hidden = true; clear(el); });
  }
  function showFieldErr(id, message) { const el = cFieldErr[id]; el.hidden = false; clear(el); el.appendChild(text(message)); }

  function openCreate(opener) {
    createOpener = opener || document.activeElement;
    clearCreateErrors();
    cFrom.value = ''; cTo.value = 'INR'; cDate.value = ''; cRate.value = ''; cSource.value = ''; cRef.value = ''; cNote.value = '';
    if (!createDialog.isConnected) document.body.appendChild(createDialog);
    createDialog.showModal();
    cFrom.focus();
  }

  createForm.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    clearCreateErrors();
    let firstInvalid = null;
    const from = cFrom.value.trim().toUpperCase();
    const to = (cTo.value.trim() || 'INR').toUpperCase();
    const rate = cRate.value.trim();
    if (!/^[A-Z]{3}$/.test(from)) { showFieldErr('from', 'A three-letter ISO 4217 code.'); firstInvalid = firstInvalid || cFrom; }
    if (!cDate.value) { showFieldErr('date', 'Effective date is required.'); firstInvalid = firstInvalid || cDate; }
    if (!/^\d+(\.\d{1,8})?$/.test(rate)) { showFieldErr('rate', 'A positive decimal with at most eight places, e.g. 83.72.'); firstInvalid = firstInvalid || cRate; }
    if (!cSource.value.trim()) { showFieldErr('source', 'Say where the rate came from.'); firstInvalid = firstInvalid || cSource; }
    if (firstInvalid) { firstInvalid.focus(); return; }
    cSubmit.disabled = true; cSubmit.textContent = 'Recording…';
    try {
      const res = await createRate({
        from_currency: from, to_currency: to, rate_date: cDate.value, rate,
        rate_source: cSource.value.trim(),
        source_reference: cRef.value.trim() || undefined,
        note: cNote.value.trim() || undefined,
      }, newIdempotencyKey());
      createDialog.close();
      announce(res && res.created === false
        ? `That rate was already on file as ${res.fx_rate_id}.`
        : `Rate ${res.fx_rate_id} recorded, pending activation by another user.`);
      loadList();
    } catch (err) {
      const detail = err instanceof FxApiError ? problemDetail(err) : { message: 'This rate could not be recorded. Try again.', code: null };
      cError.hidden = false; clear(cError);
      cError.appendChild(h('div', {}, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
        h('div', { class: 'body' }, [detail.code ? h('span', { class: 'mono fx-code' }, `${detail.code} `) : null, text(detail.message)].filter(Boolean)),
      ]));
    } finally {
      cSubmit.disabled = false; cSubmit.textContent = 'Record (pending activation)';
    }
  });

  /* ---------------- activate / retire confirmation ---------------- */

  const confirmTitle = h('h2', { id: 'fxConfirmTitle' }, '');
  const confirmClose = h('button', { type: 'button', class: 'dlg-close', 'aria-label': 'Close dialog', onClick: () => confirmDialog.close() }, '✕');
  const confirmBody = h('div', { class: 'dlg-body' });
  const confirmErrorHost = h('div', { class: 'dlg-msg' });
  const confirmReason = h('textarea', { id: 'fxRetireReason', rows: '2' });
  const confirmReasonField = h('div', { class: 'field', hidden: true }, [h('label', { for: 'fxRetireReason' }, 'Reason *'), confirmReason]);
  const confirmBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => runConfirm() }, 'Confirm');
  const confirmDialog = h('dialog', { 'aria-labelledby': 'fxConfirmTitle' }, [
    h('div', { class: 'dlg-head' }, [confirmTitle, confirmClose]),
    confirmErrorHost, confirmBody, confirmReasonField,
    h('div', { class: 'dlg-foot' }, [h('button', { type: 'button', class: 'btn-sm', onClick: () => confirmDialog.close() }, 'Cancel'), confirmBtn]),
  ]);
  let confirmRow = null;
  let confirmMode = 'activate';
  let confirmOpener = null;
  confirmDialog.addEventListener('close', () => { if (confirmOpener && typeof confirmOpener.focus === 'function') confirmOpener.focus(); confirmOpener = null; });

  function openActivate(row, opener) {
    confirmRow = row; confirmMode = 'activate'; confirmOpener = opener || document.activeElement;
    clear(confirmErrorHost); clear(confirmBody);
    confirmTitle.textContent = `Activate ${row.from_currency}/${row.to_currency} ${row.rate} for ${formatSettingsDate(row.rate_date)}`;
    confirmBody.appendChild(text(`This puts the ${row.rate_source} quote into force for ${formatSettingsDate(row.rate_date)}. `
      + 'Any active quote for the same pair, date and source is superseded (kept, marked inactive, pointing at this one). '
      + `The server refuses this if you recorded the rate yourself (${row.created_by}): activation needs a different user.`));
    confirmReasonField.hidden = true;
    confirmBtn.className = 'btn-sm btn-primary'; confirmBtn.textContent = 'Activate';
    if (!confirmDialog.isConnected) document.body.appendChild(confirmDialog);
    confirmDialog.showModal();
    confirmBtn.focus();
  }

  function openRetire(row, opener) {
    confirmRow = row; confirmMode = 'retire'; confirmOpener = opener || document.activeElement;
    clear(confirmErrorHost); clear(confirmBody);
    confirmTitle.textContent = `Retire ${row.from_currency}/${row.to_currency} ${row.rate} for ${formatSettingsDate(row.rate_date)}`;
    confirmBody.appendChild(text('A retired rate translates nothing: a document dated this day will be REFUSED until another quote is recorded and activated. '
      + 'The row and its value stay in history, and a retired rate cannot be reactivated.'));
    confirmReason.value = '';
    confirmReasonField.hidden = false;
    confirmBtn.className = 'btn-sm btn-danger'; confirmBtn.textContent = 'Retire';
    if (!confirmDialog.isConnected) document.body.appendChild(confirmDialog);
    confirmDialog.showModal();
    confirmReason.focus();
  }

  async function runConfirm() {
    if (!confirmRow) return;
    clear(confirmErrorHost);
    if (confirmMode === 'retire' && !confirmReason.value.trim()) {
      confirmErrorHost.appendChild(msgBox('warning', h('div', {}, 'A reason is required to retire a rate.')));
      confirmReason.focus();
      return;
    }
    confirmBtn.disabled = true;
    try {
      if (confirmMode === 'activate') {
        const res = await activateRate(confirmRow.fx_rate_id);
        announce(`Rate ${confirmRow.fx_rate_id} activated${res && res.superseded && res.superseded.length ? `, superseding ${res.superseded.join(', ')}` : ''}.`);
      } else {
        await deactivateRate(confirmRow.fx_rate_id, confirmReason.value.trim());
        announce(`Rate ${confirmRow.fx_rate_id} retired.`);
      }
      confirmDialog.close();
      loadList();
    } catch (err) {
      const detail = err instanceof FxApiError ? problemDetail(err) : { message: 'This action could not be completed. Try again.', code: null };
      confirmErrorHost.appendChild(msgBox('error', h('div', {}, [
        detail.code ? h('span', { class: 'mono fx-code' }, `${detail.code} `) : null, text(detail.message),
      ].filter(Boolean)), { alert: true }));
    } finally {
      confirmBtn.disabled = false;
    }
  }

  /* ---------------- import dialog ---------------- */

  const iTitle = h('h2', { id: 'fxImportTitle' }, 'Import exchange rates');
  const iClose = h('button', { type: 'button', class: 'dlg-close', 'aria-label': 'Close dialog', onClick: () => importDialog.close() }, '✕');
  const iText = h('textarea', { id: 'fxImportText', rows: '8', spellcheck: 'false', placeholder: `from_currency,to_currency,rate_date,rate,rate_source,source_reference\nUSD,INR,2026-08-06,83.85,RBI_REFERENCE,RBI bulletin 2026-08-06` });
  const iMsg = h('div', { class: 'dlg-msg' });
  const iPreviewHost = h('div', { class: 'fx-import-preview' });
  const iPreviewBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => runPreview() }, 'Preview');
  const iCommitBtn = h('button', { type: 'button', class: 'btn-sm btn-primary', disabled: true, onClick: () => runCommit() }, 'Import');
  const importDialog = h('dialog', { 'aria-labelledby': 'fxImportTitle', class: 'fx-dialog-wide' }, [
    h('div', { class: 'dlg-head' }, [iTitle, iClose]),
    iMsg,
    h('div', { class: 'dlg-body' }, [
      h('p', { class: 'fx-hint' }, 'Paste a JSON array of rate objects, or CSV with a header row. Every row is validated before any is written: one problem imports nothing. Imported rates await activation. Re-importing the same rows creates nothing.'),
      h('div', { class: 'field' }, [h('label', { for: 'fxImportText' }, 'Rows'), iText]),
      iPreviewHost,
    ]),
    h('div', { class: 'dlg-foot' }, [
      h('button', { type: 'button', class: 'btn-sm', onClick: () => importDialog.close() }, 'Cancel'),
      iPreviewBtn, iCommitBtn,
    ]),
  ]);
  let importOpener = null;
  let importRows = null;
  importDialog.addEventListener('close', () => { if (importOpener && typeof importOpener.focus === 'function') importOpener.focus(); importOpener = null; });

  const previewTable = createDataTable({
    caption: 'Import preview — what committing would do to each row',
    emptyMessage: 'Nothing to preview.',
    columns: [
      { key: 'row', label: '#', numeric: true, render: (r) => text(String(r.row)) },
      { key: 'pair', label: 'Pair', render: (r) => h('span', { class: 'mono' }, r.from_currency ? `${r.from_currency}/${r.to_currency}` : '—') },
      { key: 'rate_date', label: 'Effective', render: (r) => text(r.rate_date ? formatSettingsDate(r.rate_date) : '—') },
      { key: 'rate', label: 'Rate', numeric: true, render: (r) => h('span', { class: 'mono fx-rate' }, r.rate || '—') },
      { key: 'rate_source', label: 'Source', render: (r) => text(r.rate_source || '—') },
      {
        key: 'action',
        label: 'Outcome',
        render: (r) => {
          if (r.action === 'error') return h('span', { class: 'st-negative' }, `${r.problem.code}: ${r.problem.message}`);
          if (r.action === 'existing') return statusChip({ label: `Already on file (${r.fx_rate_id})`, tone: 'neutral' });
          if (r.action === 'correction') return statusChip({ label: `Correction of ${r.corrects} (${r.current_rate})`, tone: 'warning' });
          return statusChip({ label: 'Will be created (pending)', tone: 'positive' });
        },
      },
    ],
  });

  function openImport(opener) {
    importOpener = opener || document.activeElement;
    clear(iMsg); clear(iPreviewHost); importRows = null; iCommitBtn.disabled = true;
    if (!importDialog.isConnected) document.body.appendChild(importDialog);
    importDialog.showModal();
    iText.focus();
  }

  async function runPreview() {
    clear(iMsg); clear(iPreviewHost); importRows = null; iCommitBtn.disabled = true;
    const parsed = parseImportText(iText.value);
    if (parsed.error) { iMsg.appendChild(msgBox('warning', h('div', {}, parsed.error))); iText.focus(); return; }
    iPreviewBtn.disabled = true; iPreviewBtn.textContent = 'Previewing…';
    try {
      const res = await previewImport(parsed.rows);
      iPreviewHost.appendChild(previewTable.el);
      previewTable.renderRows(res.rows || []);
      if (res.valid) {
        importRows = parsed.rows;
        iCommitBtn.disabled = res.would_create === 0;
        iMsg.appendChild(msgBox(res.would_create ? 'info' : 'warning', h('div', {},
          `${res.would_create} row(s) will be created as pending, ${res.would_skip} already on file.`)));
      } else {
        iMsg.appendChild(msgBox('error', h('div', {}, `${res.problems.length} row problem(s). Nothing will be imported until every row is clean.`), { alert: true }));
      }
    } catch (err) {
      const detail = err instanceof FxApiError ? problemDetail(err) : { message: 'The preview could not be completed.', code: null };
      iMsg.appendChild(msgBox('error', h('div', {}, [detail.code ? h('span', { class: 'mono fx-code' }, `${detail.code} `) : null, text(detail.message)].filter(Boolean)), { alert: true }));
    } finally {
      iPreviewBtn.disabled = false; iPreviewBtn.textContent = 'Preview';
    }
  }

  async function runCommit() {
    if (!importRows) return;
    clear(iMsg);
    iCommitBtn.disabled = true; iCommitBtn.textContent = 'Importing…';
    try {
      const res = await commitImport(importRows, newIdempotencyKey());
      importDialog.close();
      announce(`Imported ${res.created} rate(s) as pending; ${res.skipped} already on file.`);
      loadList();
    } catch (err) {
      const detail = err instanceof FxApiError ? problemDetail(err) : { message: 'The import could not be completed.', code: null, problems: null };
      const lines = [detail.code ? h('span', { class: 'mono fx-code' }, `${detail.code} `) : null, text(detail.message)].filter(Boolean);
      if (detail.problems) {
        lines.push(h('ul', { class: 'fx-problems' }, detail.problems.map((p) => h('li', {}, `Row ${p.row}${p.field ? ` (${p.field})` : ''}: ${p.code} — ${p.message}`))));
      }
      iMsg.appendChild(msgBox('error', h('div', {}, lines), { alert: true }));
      iCommitBtn.disabled = false;
    } finally {
      iCommitBtn.textContent = 'Import';
    }
  }

  /* ---------------- boot ---------------- */

  (async function boot() {
    const principal = await getPrincipal();
    state.permissions = principal.permissions;
    newBtn.hidden = !can('fx.manage');
    importBtn.hidden = !can('fx.manage');
    loadList();
  }());
}
