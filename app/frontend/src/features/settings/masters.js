/* app/frontend/src/features/settings/masters.js
   SCR-30 Master Data Configuration — item and vendor masters.
   docs/WAVE2_CONTRACTS.md "API contract — Settings and masters" fixes the
   shape (source badge, external id/last-sync, masked vendor tax identity,
   duplicate_of as a review-only affordance). research/30_contracts/
   C14_traceability.json (REQ-INT-025 / REQ-INT-026) is the source for the
   vendor-only field names gst_treatment and place_of_contact used here
   alongside the masked gst_no/pan_no pair — the frozen API contract itself
   only says "..." for area-specific fields, so nothing beyond what these
   two documents name is assumed.

   Wired to the real API only (api.js) — no static fake data. Tests
   intercept the network with Playwright's page.route().
*/

import { h, text, clear } from '../../core/dom.js';
import {
  listMasters, createMaster, updateMaster, deactivateMaster, listDuplicates, SettingsApiError,
} from './api.js';
import { formatSettingsTimestamp } from './format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { sourceBadge, externalDetail } from '../../components/settings/capex-source-badge.js';
import { maskedIdentity } from '../../components/settings/capex-mask.js';
import { createRecordDialog } from '../../components/settings/capex-record-dialog.js';
import { createConfirmDialog } from '../../components/settings/capex-confirm-dialog.js';

const PAGE_SIZE = 50;

const KIND_DEF = {
  items: { idField: 'item_id', label: 'Items', singular: 'item' },
  vendors: { idField: 'vendor_id', label: 'Vendors', singular: 'vendor' },
};

function activeChip(isActive) {
  return isActive
    ? statusChip({ label: 'Active', tone: 'positive' })
    : statusChip({ label: 'Inactive', tone: 'neutral' });
}

/** "The ERP owns" code/name and, for vendors, the tax-identity fields — a ZOHO row is read-only for those. */
function zohoReadOnlyReason(row) {
  if (!row || row.source !== 'ZOHO') return false;
  return `This value is synchronised from ${row.external_source || 'Zoho'} (external id ${row.external_id || '—'}) `
    + 'and is owned by that system. Edit it there; a local override is not offered for a synced field.';
}

function rowDetail(row, kind) {
  const known = new Set([
    KIND_DEF[kind].idField, 'code', 'name', 'is_active', 'source', 'external_source', 'external_id',
    'external_last_modified', 'source_of_truth_status', 'duplicate_of', 'mapping_status',
    'version_no', 'created_at', 'created_by', 'updated_at', 'updated_by',
    'gst_no', 'pan_no', 'gst_treatment', 'place_of_contact',
  ]);
  const extraKeys = Object.keys(row || {}).filter((k) => !known.has(k));
  const rows = [
    ['Source-of-truth status', row.source_of_truth_status || '—'],
    ['Mapping status', row.mapping_status || '—'],
    ['Duplicate of', row.duplicate_of || 'No duplicate flagged'],
    ['Created', `${row.created_by || '—'} · ${formatSettingsTimestamp(row.created_at)}`],
    ['Last updated', `${row.updated_by || '—'} · ${formatSettingsTimestamp(row.updated_at)}`],
    ['Version', String(row.version_no ?? '—')],
  ];
  if (kind === 'vendors') {
    rows.push(['GST treatment', row.gst_treatment || '—']);
    rows.push(['Place of contact', row.place_of_contact || '—']);
  }
  const dl = h('dl', { class: 'kv' }, rows.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
  if (extraKeys.length) {
    extraKeys.forEach((k) => {
      dl.appendChild(h('dt', {}, k));
      dl.appendChild(h('dd', {}, row[k] === null || row[k] === undefined || row[k] === '' ? '—' : String(row[k])));
    });
  }
  return dl;
}

export function mountMasters(root) {
  if (!root) return;
  const liveRegion = document.getElementById('settingsLiveRegion');

  const state = {
    kind: 'items',
    q: '',
    source: '', // '' | LOCAL | IMPORT | ZOHO
    mappingStatus: '',
    activeFilter: 'active',
    items: [],
    cursor: null,
    hasMore: false,
    loadingMore: false,
    listState: 'loading',
    listError: null,
    duplicates: [],
    duplicatesState: 'loading', // 'loading' | 'empty' | 'error' | 'ready'
    duplicatesError: null,
  };

  function fieldsForKind(kind) {
    const base = [
      { key: 'code', label: 'Code', type: 'text', required: true, readOnly: zohoReadOnlyReason },
      { key: 'name', label: 'Name', type: 'text', required: true, readOnly: zohoReadOnlyReason },
    ];
    if (kind === 'vendors') {
      base.push(
        { key: 'gst_no', label: 'GSTIN', type: 'text', readOnly: zohoReadOnlyReason, hint: 'Masked unless you hold the reveal permission.' },
        { key: 'pan_no', label: 'PAN', type: 'text', readOnly: zohoReadOnlyReason, hint: 'Masked unless you hold the reveal permission.' },
        { key: 'gst_treatment', label: 'GST treatment', type: 'text', readOnly: zohoReadOnlyReason },
        { key: 'place_of_contact', label: 'Place of contact', type: 'text', readOnly: zohoReadOnlyReason },
      );
    }
    return base;
  }

  let dialog = createRecordDialog({ formFields: fieldsForKind('items') });
  const confirmDialog = createConfirmDialog();

  function rebuildDialogForKind(kind) {
    dialog = createRecordDialog({ formFields: fieldsForKind(kind) });
  }

  /* ---------------- kind toggle + toolbar ---------------- */

  const itemsTabBtn = h('button', { type: 'button', 'aria-pressed': 'true', onClick: () => setKind('items') }, 'Items');
  const vendorsTabBtn = h('button', { type: 'button', 'aria-pressed': 'false', onClick: () => setKind('vendors') }, 'Vendors');
  const kindToggle = h('div', { class: 'view-toggle', role: 'group', 'aria-label': 'Master data type' }, [itemsTabBtn, vendorsTabBtn]);

  const qInput = h('input', { id: 'mastersSearchInput', type: 'text', autocomplete: 'off' });
  const sourceSelect = h('select', { id: 'mastersSourceSelect' }, [
    h('option', { value: '' }, 'All sources'),
    h('option', { value: 'LOCAL' }, 'LOCAL'),
    h('option', { value: 'IMPORT' }, 'IMPORT'),
    h('option', { value: 'ZOHO' }, 'ZOHO'),
  ]);
  const mappingInput = h('input', { id: 'mastersMappingInput', type: 'text', autocomplete: 'off' });
  const activeSelect = h('select', { id: 'mastersActiveSelect' }, [
    h('option', { value: 'active' }, 'Active only'),
    h('option', { value: 'inactive' }, 'Inactive only'),
    h('option', { value: 'all' }, 'All'),
  ]);
  const applyBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Apply filters');
  const clearBtn = h('button', {
    type: 'button',
    class: 'btn-sm btn-ghost',
    onClick: () => {
      qInput.value = ''; sourceSelect.value = ''; mappingInput.value = ''; activeSelect.value = 'active';
      state.q = ''; state.source = ''; state.mappingStatus = ''; state.activeFilter = 'active';
      loadItems(true);
    },
  }, 'Clear filters');
  const newBtn = h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: (ev) => openCreateDialog(ev.currentTarget) }, 'New item');

  const filterForm = h('form', {
    class: 'toolbar',
    onSubmit: (ev) => {
      ev.preventDefault();
      state.q = qInput.value.trim();
      state.source = sourceSelect.value;
      state.mappingStatus = mappingInput.value.trim();
      state.activeFilter = activeSelect.value;
      loadItems(true);
    },
  }, [
    h('div', { class: 'field f2' }, [h('label', { for: 'mastersSearchInput' }, 'Search code or name'), qInput]),
    h('div', { class: 'field' }, [h('label', { for: 'mastersSourceSelect' }, 'Source'), sourceSelect]),
    h('div', { class: 'field' }, [h('label', { for: 'mastersMappingInput' }, 'Mapping status contains'), mappingInput]),
    h('div', { class: 'field' }, [h('label', { for: 'mastersActiveSelect' }, 'Status'), activeSelect]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), applyBtn]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, ''), clearBtn]),
    h('div', { class: 'field field-action grow-action' }, [h('span', { class: 'sr-only' }, ''), newBtn]),
  ]);

  /* ---------------- duplicates panel ---------------- */

  const duplicatesBody = h('div', { id: 'mastersDuplicatesBody' });
  const duplicatesCard = h('div', { class: 'card' }, [
    h('h3', {}, ['Possible duplicates', h('span', { class: 'spacer' }),
      h('span', { class: 'xs muted' }, 'Review only — nothing here is merged automatically.')]),
    h('div', { class: 'card-body' }, duplicatesBody),
  ]);

  function renderDuplicates() {
    clear(duplicatesBody);
    if (state.duplicatesState === 'loading') {
      duplicatesBody.appendChild(h('div', { class: 'loading' }, 'Checking for duplicate candidates…'));
      return;
    }
    if (state.duplicatesState === 'error') {
      duplicatesBody.appendChild(h('div', { class: 'xs muted' },
        (state.duplicatesError && state.duplicatesError.message) || 'Duplicate candidates could not be loaded.'));
      return;
    }
    if (state.duplicatesState === 'empty') {
      duplicatesBody.appendChild(h('div', { class: 'xs muted' }, 'No duplicate candidates flagged for this type.'));
      return;
    }
    const dupTable = createDataTable({
      caption: 'Duplicate candidates',
      emptyMessage: 'No duplicate candidates flagged.',
      columns: [
        { key: 'code', label: 'Code', render: (r) => h('span', { class: 'mono' }, r.code || '—') },
        { key: 'name', label: 'Name', render: (r) => text(r.name || '—') },
        { key: 'duplicate_of', label: 'Possible duplicate of', render: (r) => h('span', { class: 'mono' }, r.duplicate_of || '—') },
        { key: 'reason', label: 'Reason', render: (r) => text(r.reason || '—') },
        {
          key: 'locate',
          label: 'Actions',
          render: (r) => h('button', {
            type: 'button',
            class: 'btn-sm linkish',
            onClick: () => { qInput.value = r.code || ''; state.q = r.code || ''; loadItems(true); },
          }, 'Locate in list'),
        },
      ],
    });
    dupTable.renderRows(state.duplicates);
    duplicatesBody.appendChild(dupTable.el);
  }

  /* ---------------- table ---------------- */

  const statusHost = h('div', { id: 'mastersStatusHost' });

  function buildColumns() {
    const cols = [
      { key: 'code', label: 'Code', render: (row) => h('span', { class: 'mono' }, row.code || '—') },
      { key: 'name', label: 'Name', render: (row) => text(row.name || '—') },
      {
        key: 'source',
        label: 'Source',
        render: (row) => h('div', { class: 'source-cell' }, [sourceBadge(row), externalDetail(row)].filter(Boolean)),
      },
    ];
    if (state.kind === 'vendors') {
      cols.push(
        { key: 'gst_no', label: 'GSTIN', render: (row) => maskedIdentity(row.gst_no, { fieldLabel: 'GSTIN' }) },
        { key: 'pan_no', label: 'PAN', render: (row) => maskedIdentity(row.pan_no, { fieldLabel: 'PAN' }) },
      );
    }
    cols.push(
      { key: 'status', label: 'Status', render: (row) => activeChip(row.is_active) },
      {
        key: 'actions',
        label: 'Actions',
        render: (row) => h('div', { class: 'row-actions' }, [
          h('button', { type: 'button', class: 'btn-sm', onClick: (ev) => openEditDialog(row, ev.currentTarget) }, 'Edit'),
          row.is_active
            ? h('button', { type: 'button', class: 'btn-sm btn-danger', onClick: (ev) => openDeactivate(row, ev.currentTarget) }, 'Deactivate')
            : null,
        ].filter(Boolean)),
      },
    );
    return cols;
  }

  let table = createDataTable({
    caption: 'Item masters',
    emptyMessage: 'No records match these filters.',
    renderRowDetail: (row) => rowDetail(row, state.kind),
    columns: buildColumns(),
  });

  const loadMoreBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => loadItems(false) }, 'Load more');
  const paginationInfo = h('span', { class: 'muted small' }, '');
  const pagination = h('div', { class: 'settings-pagination', hidden: true }, [
    paginationInfo, h('span', { class: 'grow' }), loadMoreBtn,
  ]);

  // A real (if visually hidden) heading, not just the tab label, so the
  // document's heading levels stay in order (h1 page title -> h2 section ->
  // h3 "Possible duplicates" card) for anyone navigating by heading.
  root.appendChild(h('h2', { class: 'sr-only' }, 'Item and vendor masters'));
  root.appendChild(kindToggle);
  root.appendChild(filterForm);
  root.appendChild(duplicatesCard);
  root.appendChild(statusHost);
  root.appendChild(table.el);
  root.appendChild(pagination);

  /* ---------------- helpers ---------------- */

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

  function setKind(kind) {
    if (state.kind === kind) return;
    state.kind = kind;
    itemsTabBtn.setAttribute('aria-pressed', String(kind === 'items'));
    vendorsTabBtn.setAttribute('aria-pressed', String(kind === 'vendors'));
    clear(newBtn); newBtn.appendChild(text(`New ${KIND_DEF[kind].singular}`));
    rebuildDialogForKind(kind);
    // Rebuild the table for the new column set (GSTIN/PAN only apply to vendors).
    const oldEl = table.el;
    table = createDataTable({
      caption: KIND_DEF[kind].label,
      emptyMessage: 'No records match these filters.',
      renderRowDetail: (row) => rowDetail(row, state.kind),
      columns: buildColumns(),
    });
    oldEl.replaceWith(table.el);
    state.q = ''; state.source = ''; state.mappingStatus = ''; state.activeFilter = 'active';
    qInput.value = ''; sourceSelect.value = ''; mappingInput.value = ''; activeSelect.value = 'active';
    loadItems(true);
    loadDuplicates();
  }

  /* ---------------- rendering ---------------- */

  function render() {
    clear(statusHost);
    const def = KIND_DEF[state.kind];
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
        h('div', {}, state.listError ? state.listError.message : `No ${def.label.toLowerCase()} were found for these filters.`),
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
        h('div', {}, err ? err.message : `${def.label} could not be loaded.`),
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
        h('div', {}, `No ${def.label.toLowerCase()} match these filters. Use "New ${def.singular}" above to create one, `
          + 'or adjust the filters.'),
      ]));
      pagination.hidden = true;
      return;
    }
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
      state.items = []; state.cursor = null; state.listState = 'loading'; state.listError = null;
      render();
    } else {
      state.loadingMore = true; render();
    }
    const def = KIND_DEF[state.kind];
    try {
      const isActive = state.activeFilter === 'all' ? undefined : state.activeFilter === 'active';
      const data = await listMasters(state.kind, {
        q: state.q || undefined,
        source: state.source || undefined,
        mappingStatus: state.mappingStatus || undefined,
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

  async function loadDuplicates() {
    state.duplicatesState = 'loading';
    state.duplicatesError = null;
    renderDuplicates();
    try {
      const data = await listDuplicates(state.kind);
      state.duplicates = (data && data.items) || [];
      state.duplicatesState = state.duplicates.length ? 'ready' : 'empty';
    } catch (err) {
      state.duplicatesState = 'error';
      state.duplicatesError = { message: err instanceof SettingsApiError ? err.message : null };
    }
    renderDuplicates();
  }

  /* ---------------- create / edit / deactivate ---------------- */

  function openCreateDialog(opener) {
    const def = KIND_DEF[state.kind];
    dialog.open({
      mode: 'create',
      title: `New ${def.singular}`,
      row: null,
      opener,
      onSubmit: (values) => createMaster(state.kind, values),
      onSuccess: () => { loadItems(true); announce(`${def.singular} created.`); },
    });
  }

  function openEditDialog(row, opener) {
    const def = KIND_DEF[state.kind];
    dialog.open({
      mode: 'edit',
      title: `Edit ${def.singular} ${row.code || ''}`.trim(),
      row,
      opener,
      onSubmit: (values) => updateMaster(state.kind, row[def.idField], values),
      onSuccess: () => { loadItems(true); announce(`${def.singular} updated.`); },
      onReload: async () => {
        const data = await listMasters(state.kind, { q: row.code, limit: 5 });
        const fresh = ((data && data.items) || []).find((r) => r[def.idField] === row[def.idField]);
        return fresh || row;
      },
    });
  }

  function openDeactivate(row, opener) {
    const def = KIND_DEF[state.kind];
    confirmDialog.open({
      title: `Deactivate ${def.singular} ${row.code || ''}`.trim(),
      message: `${row.name || row.code} will be marked inactive and hidden from active lists elsewhere in the `
        + 'application. This does not delete it or its history, and can be reviewed again via the "Inactive only" '
        + 'filter, but there is no undo action on this screen.',
      opener,
      onConfirm: () => deactivateMaster(state.kind, row[def.idField]),
      onSuccess: () => { loadItems(true); announce(`${def.singular} deactivated.`); },
    });
  }

  /* ---------------- boot ---------------- */

  render();
  renderDuplicates();
  loadItems(true);
  loadDuplicates();
}
