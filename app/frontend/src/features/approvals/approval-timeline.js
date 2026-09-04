/* app/frontend/src/features/approvals/approval-timeline.js
   Approval Timeline and Audit History — every recorded action on one
   instance, in order, with its hash chain.

   Contract 9: approval_action is APPEND-ONLY and hash-chained on stream
   approval:{instance_id}, using the existing frozen payload format
   prev|at|actor|action|type|id|detail. This screen therefore shows prev_hash
   and entry_hash for every entry, and shows the sequence number, because a
   timeline that renders only the human-readable action is not an audit
   history — it is a summary, and a summary cannot be verified.

   A DELEGATED action shows BOTH identities. Contract 5 refuses a decision if
   the actor OR the person acted for is a contributor, so an audit history that
   collapsed the two into one name would hide exactly the fact that rule
   exists to protect.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp, truncateHash } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { createStateHost, stateForError } from '../../components/approvals/state-host.js';
import {
  createAnnouncer, createPagination, field, textInput, card, keyValues, replace, queryParam,
} from '../../components/approvals/screen-kit.js';
import { getTimeline, listInbox } from './approvals-api.js';

const PAGE_SIZE = 50;

export function mountApprovalTimeline(root) {
  if (!root) return;
  const announce = createAnnouncer('approvalsLiveRegion');

  const state = { instanceId: queryParam('instance'), rows: [], cursor: null, hasMore: false, loading: false };

  const refInput = textInput({ value: state.instanceId });
  const refField = field('approvalTimelineRef', 'Approval reference', refInput);
  const toolbar = h('form', {
    class: 'toolbar approval-toolbar',
    'aria-label': 'Open an approval timeline by reference',
    onSubmit: (ev) => { ev.preventDefault(); load(refInput.value.trim(), true); },
  }, [
    refField.el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Open')),
  ]);

  const chainHost = h('div', { id: 'approvalTimelineChain' });

  const table = createDataTable({
    caption: 'Every recorded action on this approval instance, in sequence, with its hash chain',
    emptyMessage: 'No actions have been recorded on this approval yet.',
    columns: [
      { key: 'seq', label: 'Seq', numeric: true, render: (r) => text(String(r.seq ?? '—')) },
      { key: 'at', label: 'When', render: (r) => text(formatAuditTimestamp(r.at)) },
      {
        key: 'actor_user_id',
        label: 'Actor',
        render: (r) => (r.acting_for_user_id && r.acting_for_user_id !== r.actor_user_id
          // Both identities, always. A delegation that showed only one name
          // would conceal the pair Contract 5 checks.
          ? h('span', {}, [
            h('span', { class: 'mono' }, String(r.actor_user_id ?? '—')),
            h('span', { class: 'approval-cell-note' }, ' acting for '),
            h('span', { class: 'mono' }, String(r.acting_for_user_id)),
          ])
          : h('span', { class: 'mono' }, String(r.actor_user_id ?? '—'))),
      },
      { key: 'action', label: 'Action', render: (r) => text(r.action ?? '—') },
      { key: 'stage_no', label: 'Stage', numeric: true, render: (r) => text(String(r.stage_no ?? '—')) },
      {
        key: 'reason',
        label: 'Reason',
        render: (r) => text([r.reason_code, r.reason_text].filter(Boolean).join(' — ') || '—'),
      },
      {
        key: 'prev_hash',
        label: 'Previous hash',
        render: (r) => h('span', { class: 'mono xs', title: String(r.prev_hash ?? '') },
          truncateHash(r.prev_hash)),
      },
      {
        key: 'entry_hash',
        label: 'Entry hash',
        render: (r) => h('span', { class: 'mono xs', title: String(r.entry_hash ?? '') },
          truncateHash(r.entry_hash)),
      },
    ],
  });

  const statusHost = createStateHost({
    id: 'approvalTimelineStatusHost',
    glyph: '⧉',
    emptyMessage: 'No approval is open. Enter an approval reference above, or open one from My Approval Inbox.',
    loadingMessage: 'Loading the approval history…',
    onRetry: () => load(state.instanceId, true),
  });

  const pagination = createPagination({
    id: 'approvalTimelinePagination',
    onMore: () => load(state.instanceId, false),
  });

  root.appendChild(card('approvalTimelineTitle', 'Approval Timeline and Audit History', [
    toolbar, statusHost.el, chainHost, table.el, pagination.el,
  ]));

  function renderChain(page) {
    const chain = page && page.chain;
    if (!chain) { replace(chainHost, null); return; }
    const intact = chain.intact !== false;
    replace(chainHost, keyValues([
      ['Stream', h('span', { class: 'mono' }, String(chain.stream_key ?? '—'))],
      ['Chain', h('span', { class: `status st-${intact ? 'positive' : 'negative'}` }, [
        h('span', { class: 'sym', 'aria-hidden': 'true' }, intact ? '✔' : '✖'),
        // Stated exactly, never softened: an auditor needs the sequence where
        // the chain first breaks, not the word "issue".
        text(intact
          ? `Intact — ${chain.entries_checked ?? 0} entries verified`
          : `BROKEN at sequence ${chain.first_break_seq ?? 'unknown'} — ${chain.entries_checked ?? 0} entries checked`),
      ])],
      ['Verified at', text(formatAuditTimestamp(chain.verified_at))],
    ]));
  }

  async function load(instanceId, reset) {
    if (state.loading) return;
    state.instanceId = instanceId || '';
    refInput.value = state.instanceId;

    if (!state.instanceId) {
      // Clear the skeleton rather than just hiding it: a hidden stale
      // skeleton still matches a query and still reads as "loading".
      table.renderRows([]);
      table.el.hidden = true;
      replace(chainHost, null);
      statusHost.set('loading');
      try {
        const page = await listInbox({ limit: 1 });
        const first = Array.isArray(page && page.items) ? page.items[0] : null;
        if (first && first.instance_id) return load(first.instance_id, true);
        statusHost.set('empty');
        announce('No approval history is open.');
      } catch (err) {
        const mapped = stateForError(err, 'No approval history could be opened. Enter a reference above.');
        statusHost.set(mapped.state, mapped);
        announce(mapped.message);
      }
      return undefined;
    }

    state.loading = true;
    if (reset) {
      state.rows = []; state.cursor = null; state.hasMore = false;
      statusHost.set('loading');
      table.el.hidden = false;
      table.renderSkeleton();
      announce('Loading the approval history.');
    }
    pagination.set({ hasMore: state.hasMore, loading: true, shown: state.rows.length });

    try {
      const page = await getTimeline(state.instanceId, {
        cursor: reset ? undefined : state.cursor || undefined,
        limit: PAGE_SIZE,
      });
      const items = Array.isArray(page && page.items) ? page.items : [];
      state.rows = reset ? items : state.rows.concat(items);
      state.cursor = (page && page.next_cursor) || null;
      state.hasMore = !!(page && page.has_more);
      renderChain(page);

      if (state.rows.length === 0) {
        // Clear the skeleton rather than just hiding it: a hidden stale
        // skeleton still matches a query and still reads as "loading".
        table.renderRows([]);
        table.el.hidden = true;
        statusHost.set('empty', { message: 'No actions have been recorded on this approval yet.' });
        announce('No actions have been recorded on this approval yet.');
      } else {
        statusHost.set('ready');
        table.el.hidden = false;
        table.renderRows(state.rows);
        announce(`${state.rows.length} recorded action${state.rows.length === 1 ? '' : 's'} shown.`);
      }
    } catch (err) {
      // Clear the skeleton rather than just hiding it: a hidden stale
      // skeleton still matches a query and still reads as "loading".
      table.renderRows([]);
      table.el.hidden = true;
      replace(chainHost, null);
      const mapped = stateForError(err, 'This approval history could not be loaded. Try again.');
      statusHost.set(mapped.state, mapped);
      announce(mapped.state === 'permission'
        ? 'No approval history was found for that reference.'
        : 'This approval history could not be loaded.');
    } finally {
      state.loading = false;
      pagination.set({ hasMore: state.hasMore, loading: false, shown: state.rows.length });
    }
    return undefined;
  }

  load(state.instanceId, true);
}
