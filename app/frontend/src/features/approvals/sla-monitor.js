/* app/frontend/src/features/approvals/sla-monitor.js
   Escalation and SLA Monitor — which stages are past their due time, and what
   escalation has done about it.

   C15: an ESCALATED stage does NOT remove the original assignees. Escalation
   adds capacity; it never removes accountability. This screen therefore shows
   BOTH the original assignees and the escalation target on every escalated
   row, because a monitor that showed only the escalation target would let the
   original approver quietly off the hook — which is the opposite of what an
   SLA monitor is for.

   Overdue is carried by a symbol and a word, never by a red row alone: a
   monitor whose only signal is colour is unusable for the reviewer most likely
   to be reading it on a plant-office screen in daylight.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { approvalStatusBadge } from '../../components/approvals/approval-status-badge.js';
import { createStateHost, stateForError } from '../../components/approvals/state-host.js';
import {
  createAnnouncer, createPagination, field, select, card,
} from '../../components/approvals/screen-kit.js';
import { listSla } from './approvals-api.js';

const PAGE_SIZE = 50;

const SCOPE_OPTIONS = [
  ['true', 'Overdue only'],
  ['false', 'Every open stage with an SLA'],
];

/**
 * Hours overdue, rendered with a symbol and a word so the state is legible
 * without colour. `hours_overdue` is the server's number; this never computes
 * lateness from the browser clock, which can be wrong by hours and would then
 * disagree with the escalation the engine actually performed.
 */
function overdueCell(row) {
  const hours = row.hours_overdue;
  if (hours === null || hours === undefined) {
    return h('span', { class: 'status st-neutral' }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, '·'),
      text('Within SLA'),
    ]);
  }
  const n = Number(hours);
  if (!Number.isFinite(n) || n <= 0) {
    return h('span', { class: 'status st-positive' }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, '✔'),
      text('Within SLA'),
    ]);
  }
  const severe = n >= 24;
  return h('span', { class: `status st-${severe ? 'negative' : 'warning'}` }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, severe ? '✖' : '!'),
    text(`Overdue by ${n.toFixed(1)} h`),
  ]);
}

export function mountSlaMonitor(root) {
  if (!root) return;
  const announce = createAnnouncer('approvalsLiveRegion');

  const state = { rows: [], cursor: null, hasMore: false, loading: false };

  const scopeSel = select(SCOPE_OPTIONS, {});
  const scopeField = field('slaScope', 'Show', scopeSel);
  const toolbar = h('div', { class: 'toolbar approval-toolbar', role: 'group', 'aria-label': 'SLA filters' }, [
    scopeField.el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => load(true) }, 'Refresh')),
  ]);

  const table = createDataTable({
    caption: 'Approval stages against their service-level target, with escalation and the original assignees',
    emptyMessage: 'No approval stage is past its due time.',
    columns: [
      {
        key: 'instance_id',
        label: 'Reference',
        render: (r) => h('a', {
          class: 'linkish approval-link',
          href: `?instance=${encodeURIComponent(r.instance_id)}#approval-request`,
        }, String(r.instance_id ?? '—')),
      },
      { key: 'object_type', label: 'Object type', render: (r) => text(r.object_type ?? '—') },
      {
        key: 'stage',
        label: 'Stage',
        render: (r) => text(r.stage_name ? `${r.stage_no ?? '—'} · ${r.stage_name}` : String(r.stage_no ?? '—')),
      },
      { key: 'status', label: 'Stage status', render: (r) => approvalStatusBadge('stage', r.status) },
      { key: 'due_at', label: 'Due', render: (r) => text(formatAuditTimestamp(r.due_at)) },
      { key: 'hours_overdue', label: 'SLA', numeric: false, render: (r) => overdueCell(r) },
      {
        key: 'assignees',
        label: 'Still accountable',
        // The original assignees, ALWAYS. An escalated stage keeps them
        // PENDING (C15), and hiding them here would let escalation read as a
        // handover when it is an addition.
        render: (r) => ((Array.isArray(r.assignees) && r.assignees.length)
          ? h('ul', { class: 'approval-assignees' },
            r.assignees.map((a) => h('li', {}, h('span', { class: 'mono' }, String(a)))))
          : text('—')),
      },
      {
        key: 'escalated_to',
        label: 'Escalated to',
        render: (r) => {
          if (!r.escalated_at) return h('span', { class: 'approval-cell-note' }, 'Not escalated');
          const targets = Array.isArray(r.escalated_to) ? r.escalated_to : [r.escalated_to].filter(Boolean);
          return h('span', {}, [
            h('span', { class: 'sym', 'aria-hidden': 'true' }, '⇧'),
            text(' '),
            targets.length
              ? h('span', { class: 'mono' }, targets.join(', '))
              : text('escalation target not configured'),
            h('span', { class: 'approval-cell-note' }, ` · ${formatAuditTimestamp(r.escalated_at)}`),
          ]);
        },
      },
    ],
  });

  const statusHost = createStateHost({
    id: 'slaStatusHost',
    glyph: '◷',
    emptyMessage: 'No approval stage is past its due time.',
    loadingMessage: 'Loading the SLA position…',
    onRetry: () => load(true),
  });

  const pagination = createPagination({ id: 'slaPagination', onMore: () => load(false) });

  root.appendChild(card('slaTitle', 'Escalation and SLA Monitor', [
    h('p', { class: 'muted small' },
      'Escalation adds an approver; it does not remove one. An escalated stage '
      + 'keeps its original assignees, and they are listed here for exactly that '
      + 'reason.'),
    toolbar, statusHost.el, table.el, pagination.el,
  ]));

  async function load(reset) {
    if (state.loading) return;
    state.loading = true;
    if (reset) {
      state.rows = []; state.cursor = null; state.hasMore = false;
      statusHost.set('loading');
      table.el.hidden = false;
      table.renderSkeleton();
      announce('Loading the SLA position.');
    }
    pagination.set({ hasMore: state.hasMore, loading: true, shown: state.rows.length });

    try {
      const page = await listSla({
        overdue: scopeSel.value === 'true',
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
        statusHost.set('empty', {
          message: scopeSel.value === 'true'
            ? 'No approval stage is past its due time.'
            : 'No open approval stage carries a service-level target.',
        });
        announce('No approval stage is past its due time.');
      } else {
        statusHost.set('ready');
        table.el.hidden = false;
        table.renderRows(state.rows);
        const overdue = state.rows.filter((r) => Number(r.hours_overdue) > 0).length;
        announce(`${state.rows.length} stage${state.rows.length === 1 ? '' : 's'} shown, ${overdue} overdue.`);
      }
    } catch (err) {
      // Clear the skeleton rather than just hiding it: a hidden stale
      // skeleton still matches a query and still reads as "loading".
      table.renderRows([]);
      table.el.hidden = true;
      const mapped = stateForError(err, 'The SLA position could not be loaded. Try again.');
      statusHost.set(mapped.state, mapped);
      announce(mapped.state === 'permission'
        ? 'No SLA records were found.'
        : 'The SLA position could not be loaded.');
    } finally {
      state.loading = false;
      pagination.set({ hasMore: state.hasMore, loading: false, shown: state.rows.length });
    }
  }

  load(true);
}
