/* app/frontend/src/features/settings/org-hierarchy.js
   Organisation-hierarchy admin — one reusable collection screen driven by
   COLLECTIONS metadata (collections.js), covering organisations, entities,
   divisions, branches, zones, plants, locations and departments per
   docs/WAVE2_CONTRACTS.md's "API contract — Settings and masters". Location
   is deliberately just another entry in that list, with its own filter and
   its own rows — never folded into plant or zone.

   Wired to the real API only (api.js) — no static fake data. Tests
   intercept the network with Playwright's page.route().
*/

import { h, text, clear } from '../../core/dom.js';
import {
  listSettings, createSetting, updateSetting, deactivateSetting, SettingsApiError,
} from './api.js';
import { COLLECTIONS, collectionByKey, CORE_FIELD_KEYS } from './collections.js';
import { formatSettingsTimestamp } from './format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createRecordDialog } from '../../components/settings/capex-record-dialog.js';
import { createConfirmDialog } from '../../components/settings/capex-confirm-dialog.js';

const PAGE_SIZE = 50;

function activeChip(isActive) {
  return isActive
    ? statusChip({ label: 'Active', tone: 'positive' })
    : statusChip({ label: 'Inactive', tone: 'neutral' });
}

/** Every field on a row the fixed columns do not already show, rendered generically. */
function extraFieldsDetail(row, idField) {
  const known = new Set([idField, ...CORE_FIELD_KEYS, 'created_at', 'created_by', 'updated_at', 'updated_by', 'version_no']);
  const extraKeys = Object.keys(row || {}).filter((k) => !known.has(k));
  const dl = h('dl', { class: 'kv' }, [
    h('dt', {}, 'Created'),
    h('dd', {}, `${row.created_by || '—'} · ${formatSettingsTimestamp(row.created_at)}`),
    h('dt', {}, 'Last updated'),
    h('dd', {}, `${row.updated_by || '—'} · ${formatSettingsTimestamp(row.updated_at)}`),
    h('dt', {}, 'Version'),
    h('dd', { class: 'mono' }, String(row.version_no ?? '—')),
    ...extraKeys.flatMap((k) => [
      h('dt', {}, k),
      h('dd', {}, row[k] === null || row[k] === undefined || row[k] === '' ? '—' : String(row[k])),
    ]),
  ]);
  return dl;
}

export function mountOrgHierarchy(root) {
  if (!root) return;
  const liveRegion = document.getElementById('settingsLiveRegion');

  const state = {
    collection: COLLECTIONS[0].key,
    q: '',
    activeFilter: 'active', // 'all' | 'active' | 'inactive'
    items: [],
    cursor: null,
    hasMore: false,
    loadingMore: false,
    listState: 'loading', // 'loading' | 'empty' | 'error' | 'permission' | 'ready'
    listError: null,
  };

  const dialog = createRecordDialog({
    formFields: [
      { key: 'code', label: 'Code', type: 'text', required: true },
      { key: 'name', label: 'Name', type: 'text', required: true },
    ],
  });
  const confirmDialog = createConfirmDialog();

  /* ---------------- toolbar ---------------- */

  const collectionSelect = h('select', { id: 'orgCollectionSelect' },
    COLLECTIONS.map((c) => h('option', { value: c.key }, c.label)));
  const qInput = h('input', { id: 'orgSearchInput', type: 'text', autocomplete: 'off' });
  const activeSelect = h('select', { id: 'orgActiveSelect' }, [
    h('option', { value: 'active' }, 'Active only'),
    h('option', { value: 'inactive' }, 'Inactive only'),
    h('option', { value: 'all' }, 'All'),
  ]);
  const applyBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Apply filters');
  const clearBtn = h('button', {
    type: 'button',
    class: 'btn-sm btn-ghost',
    onClick: () => {
      qInput.value = '';
      activeSelect.value = 'active';
      state.activeFilter = 'active';
      loadItems(true);
    },
  }, 'Clear filters');

  const newBtn = h('button', {
    type: 'button',
    class: 'btn-primary btn-sm',
    onClick: (ev) => openCreateDialog(ev.currentTarget),
  }, 'New record');

  function refreshNewBtnLabel() {
    clear(newBtn);
    newBtn.appendChild(text(`New ${currentDef().singular}`));
  }

  const filterForm = h('form', {
    class: 'toolbar',
    onSubmit: (ev) => { ev.preventDefault(); state.q = qInput.value.trim(); state.activeFilter = activeSelect.value; loadItems(true); },
  }, [
    h('div', { class: 'field' }, [h('label', { for: 'orgCollectionSelect' }, 'Collection'), collectionSelect]),
    h('div', { class: 'field f2' }, [h('label', { for: 'orgSearchInput' }, 'Search code or name'), qInput]),
    h('div', { class: 'field' }, [h('label', { for: 'orgActiveSelect' }, 'Status'), activeSelect]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), applyBtn]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, ''), clearBtn]),
    h('div', { class: 'field field-action grow-action' }, [h('span', { class: 'sr-only' }, ''), newBtn]),
  ]);

  collectionSelect.addEventListener('change', () => {
    state.collection = collectionSelect.value;
    state.q = '';
    qInput.value = '';
    refreshNewBtnLabel();
    loadItems(true);
  });

  /* ---------------- table ---------------- */

  const statusHost = h('div', { id: 'orgStatusHost' });
  const table = createDataTable({
    caption: 'Organisation hierarchy records',
    emptyMessage: 'No records match these filters.',
    renderRowDetail: (row) => extraFieldsDetail(row, currentDef().idField),
    columns: [
      { key: 'code', label: 'Code', render: (row) => h('span', { class: 'mono' }, row.code || '—') },
      { key: 'name', label: 'Name', render: (row) => text(row.name || '—') },
      { key: 'status', label: 'Status', render: (row) => activeChip(row.is_active) },
      {
        key: 'updated',
        label: 'Last updated',
        render: (row) => h('span', { class: 'xs' }, formatSettingsTimestamp(row.updated_at)),
      },
      {
        key: 'actions',
        label: 'Actions',
        render: (row) => h('div', { class: 'row-actions' }, [
          h('button', {
            type: 'button', class: 'btn-sm', onClick: (ev) => openEditDialog(row, ev.currentTarget),
          }, 'Edit'),
          row.is_active
            ? h('button', {
              type: 'button', class: 'btn-sm btn-danger', onClick: (ev) => openDeactivate(row, ev.currentTarget),
            }, 'Deactivate')
            : null,
        ].filter(Boolean)),
      },
    ],
  });

  const loadMoreBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => loadItems(false) }, 'Load more');
  const paginationInfo = h('span', { class: 'muted small' }, '');
  const pagination = h('div', { class: 'settings-pagination', hidden: true }, [
    paginationInfo, h('span', { class: 'grow' }), loadMoreBtn,
  ]);

  // A real (if visually hidden) heading, not just the tab label, so the
  // document's heading levels stay in order (h1 page title -> h2 section)
  // for anyone navigating by heading rather than by tab.
  root.appendChild(h('h2', { class: 'sr-only' }, 'Organisation hierarchy'));
  root.appendChild(filterForm);
  root.appendChild(statusHost);
  root.appendChild(table.el);
  root.appendChild(pagination);
  refreshNewBtnLabel();

  /* ---------------- helpers ---------------- */

  function currentDef() { return collectionByKey(state.collection) || COLLECTIONS[0]; }

  function msgBox(kind, body, { messageId, alert = false } = {}) {
    const attrs = { class: `msg msg-${kind}` };
    if (alert) attrs.role = 'alert';
    const SYM = { error: '✖', warning: '!', success: '✔', info: '·' };
    return h('div', attrs, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, SYM[kind] || '·'),
      h('div', { class: 'body' }, [body, messageId ? h('div', { class: 'mid' }, messageId) : null].filter(Boolean)),
    ]);
  }

  function announce(message) { if (liveRegion) liveRegion.textContent = message; }

  /* ---------------- rendering ---------------- */

  function render() {
    clear(statusHost);
    if (state.listState === 'loading') {
      statusHost.hidden = true;
      table.el.hidden = false;
      table.renderSkeleton();
      pagination.hidden = true;
      return;
    }
    if (state.listState === 'permission') {
      statusHost.hidden = false;
      table.el.hidden = true;
      table.renderRows([]);
      statusHost.appendChild(h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '⎙'),
        h('div', {}, state.listError ? state.listError.message
          : `No ${currentDef().label.toLowerCase()} were found for these filters.`),
      ]));
      pagination.hidden = true;
      return;
    }
    if (state.listState === 'error') {
      statusHost.hidden = false;
      table.el.hidden = true;
      table.renderRows([]);
      const err = state.listError;
      statusHost.appendChild(msgBox('error', h('div', {}, [
        h('div', {}, err ? err.message : `${currentDef().label} could not be loaded.`),
        h('div', { class: 'btn-row' }, [
          h('button', { type: 'button', class: 'btn-sm', onClick: () => loadItems(true) }, 'Retry'),
        ]),
      ]), { messageId: err && err.messageId, alert: true }));
      pagination.hidden = true;
      return;
    }
    if (state.listState === 'empty') {
      statusHost.hidden = false;
      table.el.hidden = true;
      table.renderRows([]);
      statusHost.appendChild(h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '⎙'),
        h('div', {}, `No ${currentDef().label.toLowerCase()} match these filters. `
          + `Use "New ${currentDef().singular}" above to create one, or adjust the status filter.`),
      ]));
      pagination.hidden = true;
      return;
    }
    // ready
    statusHost.hidden = true;
    table.el.hidden = false;
    table.renderRows(state.items);
    pagination.hidden = false;
    paginationInfo.textContent = `${state.items.length} ${state.items.length === 1 ? 'record' : 'records'} loaded`
      + (state.hasMore ? '' : ' · all matching records loaded');
    loadMoreBtn.hidden = !state.hasMore;
    loadMoreBtn.disabled = state.loadingMore;
    loadMoreBtn.textContent = state.loadingMore ? 'Loading…' : 'Load more';
  }

  /* ---------------- data loading ---------------- */

  async function loadItems(reset) {
    if (reset) {
      state.items = [];
      state.cursor = null;
      state.listState = 'loading';
      state.listError = null;
      render();
    } else {
      state.loadingMore = true;
      render();
    }
    const def = currentDef();
    try {
      const isActive = state.activeFilter === 'all' ? undefined : state.activeFilter === 'active';
      const data = await listSettings(def.key, {
        q: state.q || undefined,
        isActive,
        cursor: reset ? undefined : state.cursor || undefined,
        limit: PAGE_SIZE,
      });
      const items = (data && data.items) || [];
      state.items = reset ? items : state.items.concat(items);
      state.cursor = data ? data.next_cursor : null;
      state.hasMore = !!(data && data.has_more);
      state.listState = state.items.length ? 'ready' : 'empty';
      state.loadingMore = false;
      announce(state.listState === 'ready' ? `${state.items.length} ${def.label.toLowerCase()} loaded.`
        : `No ${def.label.toLowerCase()} match these filters.`);
    } catch (err) {
      state.loadingMore = false;
      if (err instanceof SettingsApiError && err.kind === 'notfound') {
        state.listState = 'permission';
        state.listError = { message: err.message, messageId: err.messageId };
        announce(`No ${def.label.toLowerCase()} were found.`);
      } else {
        state.listState = 'error';
        state.listError = {
          message: err instanceof SettingsApiError ? err.message : `${def.label} could not be loaded. Try again.`,
          messageId: err instanceof SettingsApiError ? err.messageId : null,
        };
        announce(`${def.label} could not be loaded.`);
      }
    }
    render();
  }

  /* ---------------- create / edit / deactivate ---------------- */

  function openCreateDialog(opener) {
    const def = currentDef();
    dialog.open({
      mode: 'create',
      title: `New ${def.singular}`,
      row: null,
      opener,
      onSubmit: (values) => createSetting(def.key, values),
      onSuccess: () => { loadItems(true); announce(`${def.singular} created.`); },
    });
  }

  function openEditDialog(row, opener) {
    const def = currentDef();
    dialog.open({
      mode: 'edit',
      title: `Edit ${def.singular} ${row.code || ''}`.trim(),
      row,
      opener,
      onSubmit: (values) => updateSetting(def.key, row[def.idField], values),
      onSuccess: () => { loadItems(true); announce(`${def.singular} updated.`); },
      onReload: async () => {
        // No single-record GET is defined by the frozen contract, so the
        // freshest available copy is refetched from the list itself.
        const data = await listSettings(def.key, { q: row.code, limit: 5 });
        const fresh = ((data && data.items) || []).find((r) => r[def.idField] === row[def.idField]);
        return fresh || row;
      },
    });
  }

  function openDeactivate(row, opener) {
    const def = currentDef();
    confirmDialog.open({
      title: `Deactivate ${def.singular} ${row.code || ''}`.trim(),
      message: `${row.name || row.code} will be marked inactive and hidden from active lists elsewhere in the `
        + 'application. This does not delete it or its history, and can be reviewed again via the "Inactive only" '
        + 'filter, but there is no undo action on this screen.',
      opener,
      onConfirm: () => deactivateSetting(def.key, row[def.idField]),
      onSuccess: () => { loadItems(true); announce(`${def.singular} deactivated.`); },
    });
  }

  /* ---------------- boot ---------------- */

  render();
  loadItems(true);
}
