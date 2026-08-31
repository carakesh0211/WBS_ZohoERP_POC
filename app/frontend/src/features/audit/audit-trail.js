/* app/frontend/src/features/audit/audit-trail.js
   SCR-28 Audit Trail Viewer, with hash-chain verification.

   Wired to the real API only — GET /api/audit/streams, /api/audit/entries,
   /api/audit/chain/verify (see core/api.js for the exact contract). There is no
   static fake data anywhere in this module; every row on screen came back
   from a fetch() call. Tests intercept the network with Playwright's
   page.route() rather than this module ever hard-coding a row.

   Mounts into a host element (id="audit-trail-root" by default) so it can be
   dropped into any host page — see app/frontend/audit.html.
*/

import { listStreams, listEntries, verifyChain, AuditApiError } from '../../core/api.js';
import { h, text, clear } from '../../core/dom.js';
import { formatAuditTimestamp, truncateHash } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { createTimeline } from '../../components/capex-timeline.js';

const PAGE_SIZE = 50;
const MSG_ICON = { error: '✖', warning: '!', success: '✔', info: '·' };

/** The .msg / .msg-<kind> pattern from styles.css, reused exactly. */
function msgBox(kind, body, { messageId, alert = false } = {}) {
  const attrs = { class: `msg msg-${kind}` };
  if (alert) attrs.role = 'alert';
  return h('div', attrs, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, MSG_ICON[kind] || '·'),
    h('div', { class: 'body' }, [
      body,
      messageId ? h('div', { class: 'mid' }, messageId) : null,
    ].filter(Boolean)),
  ]);
}

function emptyBlock(message) {
  return h('div', { class: 'empty' }, [
    h('div', { class: 'big', 'aria-hidden': 'true' }, '⎙'),
    h('div', {}, message),
  ]);
}

function announce(liveRegion, message) {
  if (liveRegion) liveRegion.textContent = message;
}

export function mountAuditTrail(root) {
  if (!root) return;
  const liveRegion = document.getElementById('auditLiveRegion');

  const state = {
    streams: [],
    streamKey: '',
    objectType: '',
    objectId: '',
    entries: [],
    cursor: null,
    hasMore: false,
    loadingMore: false,
    entriesState: 'loading', // 'loading' | 'empty' | 'error' | 'permission' | 'ready'
    entriesError: null,
    view: 'table', // 'table' | 'timeline'
    verify: null,
    verifyLoading: false,
    verifyError: null,
  };

  /* ---------------- static chrome, built once ---------------- */

  const streamSelect = h('select', { id: 'auditStreamSelect' }, [
    h('option', { value: '' }, 'All streams'),
  ]);
  const objectTypeInput = h('input', { id: 'auditObjectType', type: 'text', autocomplete: 'off' });
  const objectIdInput = h('input', { id: 'auditObjectId', type: 'text', autocomplete: 'off' });

  const applyBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Apply filters');
  const clearBtn = h('button', {
    type: 'button',
    class: 'btn-sm btn-ghost',
    onClick: () => {
      streamSelect.value = '';
      objectTypeInput.value = '';
      objectIdInput.value = '';
      applyFilters();
    },
  }, 'Clear filters');

  const filterForm = h('form', {
    class: 'toolbar',
    onSubmit: (ev) => { ev.preventDefault(); applyFilters(); },
  }, [
    h('div', { class: 'field' }, [
      h('label', { for: 'auditStreamSelect' }, 'Audit stream'),
      streamSelect,
    ]),
    h('div', { class: 'field' }, [
      h('label', { for: 'auditObjectType' }, 'Object type'),
      objectTypeInput,
    ]),
    h('div', { class: 'field' }, [
      h('label', { for: 'auditObjectId' }, 'Object ID'),
      objectIdInput,
    ]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), applyBtn]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, ''), clearBtn]),
  ]);

  function applyFilters() {
    state.streamKey = streamSelect.value;
    state.objectType = objectTypeInput.value.trim();
    state.objectId = objectIdInput.value.trim();
    loadEntries(true);
    loadVerify();
  }

  const tableViewBtn = h('button', {
    type: 'button', 'aria-pressed': 'true',
    onClick: () => setView('table'),
  }, 'Table');
  const timelineViewBtn = h('button', {
    type: 'button', 'aria-pressed': 'false',
    onClick: () => setView('timeline'),
  }, 'Timeline');
  const viewToggle = h('div', { class: 'view-toggle', role: 'group', 'aria-label': 'Display as' },
    [tableViewBtn, timelineViewBtn]);

  const reverifyBtn = h('button', {
    type: 'button', class: 'btn-sm',
    onClick: () => loadVerify(),
  }, 'Re-verify chain');

  const toolbarRow = h('div', { class: 'btn-row' }, [viewToggle, reverifyBtn]);

  const chainArea = h('div', { id: 'auditChainArea' });

  const table = createDataTable({
    caption: 'Audit trail entries',
    emptyMessage: 'No audit entries match these filters.',
    renderRowDetail: (row) => h('dl', { class: 'kv' }, [
      h('dt', {}, 'Correlation ID'),
      h('dd', { class: 'mono' }, row.correlation_id || '—'),
      h('dt', {}, 'Entry hash'),
      h('dd', { class: 'mono' }, row.entry_hash || '—'),
      h('dt', {}, 'Full detail'),
      h('dd', {}, row.detail || '—'),
    ]),
    columns: [
      { key: 'seq', label: 'Seq', numeric: true, render: (row) => text(row.seq) },
      { key: 'at', label: 'Timestamp', render: (row) => text(formatAuditTimestamp(row.at)) },
      { key: 'actor', label: 'Actor', render: (row) => text(row.actor || '—') },
      { key: 'action', label: 'Action', render: (row) => text(row.action || '—') },
      {
        key: 'object',
        label: 'Object',
        render: (row) => text(row.object_type ? `${row.object_type} ${row.object_id ?? ''}`.trim() : '—'),
      },
      {
        key: 'detail',
        label: 'Detail',
        render: (row) => h('span', { class: 'audit-detail-cell', title: row.detail || '' },
          row.detail ? (row.detail.length > 80 ? `${row.detail.slice(0, 80)}…` : row.detail) : '—'),
      },
      {
        key: 'entry_hash',
        label: 'Entry hash',
        render: (row) => h('span', { class: 'audit-hash-cell mono', title: row.entry_hash || '' },
          truncateHash(row.entry_hash)),
      },
    ],
  });

  const timelineHost = h('div', { id: 'auditTimelineHost', hidden: true });
  const statusHost = h('div', { id: 'auditStatusHost' });
  const listPanel = h('div', { id: 'auditListPanel' }, [statusHost, table.el, timelineHost]);

  const loadMoreBtn = h('button', {
    type: 'button', class: 'btn-sm',
    onClick: () => loadEntries(false),
  }, 'Load more');
  const paginationInfo = h('span', { class: 'muted small' }, '');
  const pagination = h('div', { class: 'audit-pagination', hidden: true }, [
    paginationInfo, h('span', { class: 'grow' }), loadMoreBtn,
  ]);

  root.appendChild(filterForm);
  root.appendChild(chainArea);
  root.appendChild(toolbarRow);
  root.appendChild(listPanel);
  root.appendChild(pagination);

  /* ---------------- rendering ---------------- */

  function setView(view) {
    state.view = view;
    tableViewBtn.setAttribute('aria-pressed', String(view === 'table'));
    timelineViewBtn.setAttribute('aria-pressed', String(view === 'timeline'));
    renderListPanel();
  }

  function renderListPanel() {
    const showTable = state.view === 'table';
    if (state.entriesState === 'loading') {
      clear(statusHost);
      if (showTable) {
        statusHost.hidden = true;
        table.el.hidden = false;
        timelineHost.hidden = true;
        table.renderSkeleton();
      } else {
        statusHost.hidden = false;
        statusHost.appendChild(h('div', { class: 'loading' }, 'Loading audit timeline…'));
        table.el.hidden = true;
        timelineHost.hidden = true;
      }
      pagination.hidden = true;
      return;
    }

    if (state.entriesState === 'empty' || state.entriesState === 'permission') {
      clear(statusHost);
      statusHost.hidden = false;
      table.el.hidden = true;
      timelineHost.hidden = true;
      table.renderRows([]); // purge any skeleton rows left behind by the loading state
      statusHost.appendChild(emptyBlock(state.entriesError ? state.entriesError.message
        : 'No audit entries recorded yet. Entries appear here once actions are performed elsewhere in the application.'));
      pagination.hidden = true;
      return;
    }

    if (state.entriesState === 'error') {
      clear(statusHost);
      statusHost.hidden = false;
      table.el.hidden = true;
      timelineHost.hidden = true;
      table.renderRows([]); // purge any skeleton rows left behind by the loading state
      const err = state.entriesError;
      statusHost.appendChild(msgBox('error', h('div', {}, [
        h('div', {}, err ? err.message : 'The audit trail could not be loaded.'),
        h('div', { class: 'btn-row' }, [
          h('button', { type: 'button', class: 'btn-sm', onClick: () => loadEntries(true) }, 'Retry'),
        ]),
      ]), { messageId: err && err.messageId, alert: true }));
      pagination.hidden = true;
      return;
    }

    // ready
    statusHost.hidden = true;
    clear(statusHost);
    if (showTable) {
      table.el.hidden = false;
      timelineHost.hidden = true;
      table.renderRows(state.entries);
    } else {
      table.el.hidden = true;
      timelineHost.hidden = false;
      clear(timelineHost);
      timelineHost.appendChild(createTimeline({
        entries: state.entries,
        ariaLabel: 'Audit trail timeline',
      }));
    }
    pagination.hidden = false;
    paginationInfo.textContent = state.entries.length === 1
      ? '1 entry loaded'
      : `${state.entries.length} entries loaded`;
    loadMoreBtn.hidden = !state.hasMore;
    loadMoreBtn.disabled = state.loadingMore;
    loadMoreBtn.textContent = state.loadingMore ? 'Loading…' : 'Load more';
    if (!state.hasMore) {
      paginationInfo.textContent += ' · all matching entries loaded';
    }
  }

  function renderChainArea() {
    clear(chainArea);
    reverifyBtn.disabled = !state.streamKey || state.verifyLoading;

    if (!state.streamKey) {
      chainArea.appendChild(msgBox('info', 'Select a single audit stream above to verify its hash chain.'));
      return;
    }
    if (state.verifyLoading) {
      chainArea.appendChild(h('div', { class: 'loading' }, 'Verifying chain integrity…'));
      return;
    }
    if (state.verifyError) {
      chainArea.appendChild(msgBox('error', state.verifyError.message,
        { messageId: state.verifyError.messageId, alert: true }));
      return;
    }
    if (state.verify) {
      const v = state.verify;
      if (v.intact) {
        chainArea.appendChild(msgBox('success', h('div', {}, [
          h('strong', {}, 'Chain intact. '),
          text(`${v.entries_checked} ${v.entries_checked === 1 ? 'entry' : 'entries'} checked as of `
            + `${formatAuditTimestamp(v.verified_at)}.`),
        ])));
      } else {
        // Never round, soften or omit a break: the exact sequence, stated plainly.
        const seqText = v.first_break_seq !== null && v.first_break_seq !== undefined
          ? String(v.first_break_seq) : 'unknown';
        chainArea.appendChild(msgBox('error', h('div', {}, [
          h('strong', {}, `Chain broken at sequence ${seqText}. `),
          text(`${v.entries_checked} ${v.entries_checked === 1 ? 'entry was' : 'entries were'} verified `
            + `before the break. Entries from sequence ${seqText} onward cannot be verified until this `
            + 'is investigated.'),
        ]), { alert: true }));
      }
    }
  }

  /* ---------------- data loading ---------------- */

  async function loadStreams() {
    try {
      const data = await listStreams();
      state.streams = (data && data.items) || [];
      for (const s of state.streams) {
        streamSelect.appendChild(h('option', { value: s.stream_key },
          `${s.stream_key} (${s.entry_count} ${s.entry_count === 1 ? 'entry' : 'entries'})`));
      }
    } catch {
      // The stream picker is a convenience over the entries/verify endpoints,
      // which remain independently usable (e.g. by typing object filters), so
      // a failure here degrades to "All streams" only rather than blocking
      // the screen.
      streamSelect.appendChild(h('option', { value: '', disabled: true }, 'Streams unavailable'));
    }
  }

  async function loadEntries(reset) {
    if (reset) {
      state.entries = [];
      state.cursor = null;
      state.entriesState = 'loading';
      state.entriesError = null;
      renderListPanel();
    } else {
      state.loadingMore = true;
      renderListPanel();
    }

    try {
      const data = await listEntries({
        streamKey: state.streamKey || undefined,
        objectType: state.objectType || undefined,
        objectId: state.objectId || undefined,
        cursor: reset ? undefined : state.cursor || undefined,
        limit: PAGE_SIZE,
      });
      const items = (data && data.items) || [];
      state.entries = reset ? items : state.entries.concat(items);
      state.cursor = data ? data.next_cursor : null;
      state.hasMore = !!(data && data.has_more);
      state.entriesState = state.entries.length ? 'ready' : 'empty';
      state.loadingMore = false;
      announce(liveRegion, state.entriesState === 'ready'
        ? `${state.entries.length} audit entries loaded.`
        : 'No audit entries match these filters.');
    } catch (err) {
      state.loadingMore = false;
      if (err instanceof AuditApiError && err.kind === 'notfound') {
        state.entriesState = 'permission';
        state.entriesError = { message: err.message, messageId: err.messageId };
        announce(liveRegion, 'No audit trail was found for these filters.');
      } else {
        state.entriesState = 'error';
        state.entriesError = {
          message: err instanceof AuditApiError ? err.message
            : 'The audit trail could not be loaded. Try again.',
          messageId: err instanceof AuditApiError ? err.messageId : null,
        };
        announce(liveRegion, 'The audit trail could not be loaded.');
      }
    }
    renderListPanel();
  }

  async function loadVerify() {
    if (!state.streamKey) {
      state.verify = null;
      state.verifyError = null;
      renderChainArea();
      return;
    }
    state.verifyLoading = true;
    state.verifyError = null;
    renderChainArea();
    try {
      state.verify = await verifyChain(state.streamKey);
      announce(liveRegion, state.verify && state.verify.intact
        ? 'Audit chain verified intact.'
        : `Audit chain broken at sequence ${state.verify && state.verify.first_break_seq}.`);
    } catch (err) {
      state.verify = null;
      state.verifyError = {
        message: err instanceof AuditApiError ? err.message : 'The audit chain could not be verified.',
        messageId: err instanceof AuditApiError ? err.messageId : null,
      };
      announce(liveRegion, 'The audit chain could not be verified.');
    }
    state.verifyLoading = false;
    renderChainArea();
  }

  /* ---------------- boot ---------------- */

  renderChainArea();
  renderListPanel();
  loadStreams();
  loadEntries(true);
}

document.addEventListener('DOMContentLoaded', () => {
  const root = document.getElementById('audit-trail-root');
  if (root) mountAuditTrail(root);
});
