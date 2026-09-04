/* app/frontend/src/features/approvals/approval-inbox.js
   My Approval Inbox — the list of approval instances awaiting THIS caller.

   Scope and assignment are the SERVER's decision (Contract 6): the inbox query
   goes through repo.query with {scope}, and a caller sees only instances inside
   their scope and only assignments addressed to them unless they hold
   approval.configure. This screen sends filters and renders what comes back. It
   never filters a row out on the caller's behalf, because a row this client
   decided to hide would be a client-side authorization decision — and a row
   the server sent that this client hid would make the two disagree about what
   the user is entitled to see, with the client winning silently.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp, formatINR } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { approvalStatusBadge } from '../../components/approvals/approval-status-badge.js';
import { createStateHost, stateForError } from '../../components/approvals/state-host.js';
import {
  createAnnouncer, createPagination, field, select, card,
} from '../../components/approvals/screen-kit.js';
import { listInbox } from './approvals-api.js';

const PAGE_SIZE = 25;

const STATE_OPTIONS = [
  ['', 'All states'],
  ['OPEN', 'Open'],
  ['EXCEPTION_PENDING', 'Exception pending'],
  ['APPROVED', 'Approved'],
  ['REJECTED', 'Rejected'],
  ['RETURNED', 'Returned'],
  ['RECALLED', 'Recalled'],
  ['SUPERSEDED', 'Superseded'],
];

const OBJECT_TYPE_OPTIONS = [
  ['', 'All object types'],
  ['purchase_request', 'Purchase request'],
  ['purchase_order', 'Purchase order'],
  ['budget_revision', 'Budget revision'],
  ['vendor_bill', 'Vendor bill'],
  ['capitalisation', 'Capitalisation'],
];

export function mountApprovalInbox(root) {
  if (!root) return;
  const announce = createAnnouncer('approvalsLiveRegion');

  const state = {
    rows: [], cursor: null, hasMore: false, loading: false,
  };

  const stateSel = select(STATE_OPTIONS, { });
  const typeSel = select(OBJECT_TYPE_OPTIONS, { });
  const stateField = field('inboxStateFilter', 'State', stateSel);
  const typeField = field('inboxTypeFilter', 'Object type', typeSel);

  const applyBtn = h('button', {
    type: 'button', class: 'btn-primary btn-sm', onClick: () => load(true),
  }, 'Apply filters');

  const toolbar = h('div', { class: 'toolbar approval-toolbar', role: 'group', 'aria-label': 'Inbox filters' }, [
    stateField.el,
    typeField.el,
    h('div', { class: 'field field-action' }, applyBtn),
  ]);

  const table = createDataTable({
    caption: 'Approval instances awaiting a decision, with object, stage, value and due date',
    emptyMessage: 'Nothing is awaiting your decision.',
    columns: [
      {
        key: 'instance_id',
        label: 'Reference',
        render: (row) => h('a', {
          class: 'linkish approval-link',
          href: `?instance=${encodeURIComponent(row.instance_id)}#approval-request`,
        }, String(row.instance_id ?? '—')),
      },
      { key: 'object_type', label: 'Object type', render: (row) => text(row.object_type ?? '—') },
      { key: 'object_id', label: 'Object', render: (row) => h('span', { class: 'mono' }, String(row.object_id ?? '—')) },
      {
        key: 'status',
        label: 'Status',
        render: (row) => approvalStatusBadge('instance', row.status),
      },
      {
        key: 'current_stage_no',
        label: 'Stage',
        render: (row) => text(row.current_stage_name
          ? `${row.current_stage_no ?? '—'} · ${row.current_stage_name}`
          : String(row.current_stage_no ?? '—')),
      },
      {
        key: 'amount_paise',
        label: 'Value',
        numeric: true,
        render: (row) => text(formatINR(row.amount_paise)),
      },
      {
        key: 'due_at',
        label: 'Due',
        render: (row) => {
          const overdue = !!row.overdue;
          return h('span', { class: overdue ? 'approval-overdue' : '' }, [
            overdue ? h('span', { class: 'sym', 'aria-hidden': 'true' }, '!') : null,
            text(formatAuditTimestamp(row.due_at)),
            overdue ? h('span', { class: 'sr-only' }, ' (overdue)') : null,
          ].filter(Boolean));
        },
      },
      {
        key: 'maker_user_id',
        label: 'Raised by',
        render: (row) => text(row.maker_user_id ?? '—'),
      },
    ],
  });

  const statusHost = createStateHost({
    id: 'approvalInboxStatusHost',
    glyph: '✔',
    emptyMessage: 'Nothing is awaiting your decision. When an object is routed to you it will appear here.',
    loadingMessage: 'Loading your approval inbox…',
    onRetry: () => load(true),
  });

  const pagination = createPagination({
    id: 'approvalInboxPagination',
    onMore: () => load(false),
  });

  root.appendChild(card('approvalInboxTitle', 'My Approval Inbox', [
    toolbar,
    statusHost.el,
    table.el,
    pagination.el,
  ]));

  async function load(reset) {
    if (state.loading) return;
    state.loading = true;
    if (reset) {
      state.rows = [];
      state.cursor = null;
      state.hasMore = false;
      statusHost.set('loading');
      table.el.hidden = false;
      table.renderSkeleton();
      announce('Loading your approval inbox.');
    }
    pagination.set({ hasMore: state.hasMore, loading: true, shown: state.rows.length });

    try {
      const page = await listInbox({
        state: stateSel.value || undefined,
        objectType: typeSel.value || undefined,
        cursor: reset ? undefined : state.cursor || undefined,
        limit: PAGE_SIZE,
      });
      const items = Array.isArray(page && page.items) ? page.items : [];
      state.rows = reset ? items : state.rows.concat(items);
      state.cursor = (page && page.next_cursor) || null;
      state.hasMore = !!(page && page.has_more);

      if (state.rows.length === 0) {
        // Clear the skeleton rather than just hiding it: a hidden stale
        // skeleton still matches a query and still reads as "loading".
        table.renderRows([]);
        table.el.hidden = true;
        statusHost.set('empty');
        announce('Nothing is awaiting your decision.');
      } else {
        statusHost.set('ready');
        table.el.hidden = false;
        table.renderRows(state.rows);
        announce(`${state.rows.length} approval${state.rows.length === 1 ? '' : 's'} shown.`);
      }
    } catch (err) {
      // Clear the skeleton rather than just hiding it: a hidden stale
      // skeleton still matches a query and still reads as "loading".
      table.renderRows([]);
      table.el.hidden = true;
      const mapped = stateForError(err, 'Your approval inbox could not be loaded. Try again.');
      statusHost.set(mapped.state, mapped);
      announce(mapped.state === 'permission'
        ? 'No approval records were found.'
        : 'Your approval inbox could not be loaded.');
    } finally {
      state.loading = false;
      pagination.set({ hasMore: state.hasMore, loading: false, shown: state.rows.length });
    }
  }

  load(true);
}
