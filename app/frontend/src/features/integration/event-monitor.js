/* app/frontend/src/features/integration/event-monitor.js
   SCR-26 — Integration Event Monitor.

   ONE CORRELATION ID TRACES A ZOHO BILL FROM HTTP RESPONSE TO LEDGER MOVEMENT.
   §11.9: "correlation_id propagates from the existing middleware through job →
   inbox/outbox → integration_event → audit_log, so one id traces a Zoho bill
   from HTTP response to ledger movement to audit entry." That sentence is the
   reason this screen exists and the reason the correlation id is a filter, a
   column, and a link into the audit trail rather than a diagnostic string
   buried in a detail pane.

   THE STATUSES HERE ARE OPERATIONAL, AND NEVER LEAVE THIS SCREEN.
   C16: inbox / outbox / job / circuit statuses "must NEVER be rendered on a
   business screen as if they were C3 business statuses". They are rendered
   through `operationalChip(namespace, code)`, which lives in the integration
   feature and requires a namespace — a business screen has neither reason to
   import it nor a namespace to give it. The one sanctioned bridge to a
   business screen is the separate `integration_state` badge (QUEUED | SENT |
   FAILED), which is shown here alongside the operational status precisely so
   an administrator can see that they are two different facts about the same
   row.

   THE RAW EXTERNAL STATUS IS SHOWN VERBATIM AND NEVER OVERWRITTEN.
   C3/§8.4: an unmapped raw value never guesses. Where a row carries an
   `external_status_raw` that has no mapping, the raw string is displayed as
   itself, labelled unmapped, next to the `UNMAPPED_EXTERNAL_STATUS`
   reconciliation exception it raised. Substituting a plausible business status
   there is the single most expensive mistake available on this screen.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createPagination } from '../../components/approvals/screen-kit.js';
import { createLoader } from './integration-screen.js';
import {
  INTEGRATION_STATES, createAnnouncer, card, field, integrationStateBadge, modeBanner,
  operationalChip, queryParam, selectInput, textInput,
} from './integration-kit.js';
import { getGlobalMode, listEvents } from './integration-api.js';

const PAGE_SIZE = 50;

const DIRECTIONS = [
  ['', 'Every direction'],
  ['INBOUND', 'Inbound — Zoho to us'],
  ['OUTBOUND', 'Outbound — us to Zoho'],
  ['TEST', 'Connectivity test'],
];

const NAMESPACES = [
  ['', 'Every queue'],
  ['inbox', 'Inbox — received payloads'],
  ['outbox', 'Outbox — emissions'],
  ['job', 'Jobs — chunked background work'],
];

/**
 * The event's operational status, in the right namespace.
 *
 * The namespace comes from the row, never from the code: outbox PENDING and
 * job PENDING are different facts, and a chip that guessed which one it was
 * looking at would eventually guess wrong on the row that mattered.
 */
function statusCell(row) {
  const namespace = row.queue || row.namespace
    || (row.direction === 'OUTBOUND' ? 'outbox' : row.direction === 'INBOUND' ? 'inbox' : null);
  const code = row.state || row.status;
  if (!code) return text('—');
  if (!namespace) {
    // No namespace means no basis for a tone. Show it verbatim rather than
    // choosing a colour that asserts a meaning the server did not send.
    return statusChip({
      label: String(code).toUpperCase(),
      tone: 'neutral',
      title: 'The server did not say which C16 namespace this status belongs to, so no meaning is '
        + 'inferred from it.',
    });
  }
  return operationalChip(namespace, code);
}

/**
 * The business-visible bridge, when the row carries one.
 *
 * `integrationStateBadge()` throws on a C3 business status, so a server that
 * put one in this field produces a visible, attributable failure here rather
 * than a business status rendered where an integration state belongs. The
 * throw is caught per row so one bad row cannot blank the table.
 */
function bridgeCell(row) {
  const value = row.integration_state;
  if (!value) return h('span', { class: 'muted' }, '—');
  try {
    return integrationStateBadge(value);
  } catch (err) {
    return statusChip({
      label: 'INVALID',
      tone: 'negative',
      title: `The server sent integration_state="${value}", which is not one of `
        + `${INTEGRATION_STATES.join(' | ')}. ${err.message}`,
    });
  }
}

export function mountEventMonitor(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = {
    rows: [], cursor: null, hasMore: false, loading: false, mode: 'MOCK', modeNote: '',
  };

  const banner = h('div', { id: 'eventsModeBanner' });

  const directionSelect = selectInput(DIRECTIONS);
  const namespaceSelect = selectInput(NAMESPACES);
  const correlationInput = textInput({ maxlength: '64', value: queryParam('correlation') || '' });
  const moduleInput = textInput({ maxlength: '64' });

  const toolbar = h('div', { class: 'toolbar integration-toolbar', role: 'group', 'aria-label': 'Event filters' }, [
    field('eventsDirection', 'Direction', directionSelect).el,
    field('eventsNamespace', 'Queue', namespaceSelect).el,
    field('eventsModule', 'Module', moduleInput).el,
    field('eventsCorrelation', 'Correlation id', correlationInput,
      { hint: 'One id traces a document from HTTP response to ledger movement to audit entry.' }).el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => load(true) }, 'Apply')),
  ]);

  const table = createDataTable({
    caption: 'Integration events, with the operational status, the raw external status verbatim, '
      + 'and the correlation id that traces each one end to end',
    emptyMessage: 'No integration event matches these filters.',
    columns: [
      { key: 'at', label: 'At', render: (r) => text(formatAuditTimestamp(r.at || r.occurred_at || r.received_at)) },
      { key: 'direction', label: 'Direction', render: (r) => text(r.direction || '—') },
      { key: 'module', label: 'Module', render: (r) => h('span', { class: 'mono' }, String(r.module ?? '—')) },
      {
        key: 'endpoint',
        label: 'Endpoint',
        render: (r) => (r.endpoint
          ? h('span', { class: 'mono xs' }, `${r.http_method || ''} ${r.endpoint}`.trim())
          : text('—')),
      },
      { key: 'state', label: 'Operational status', render: statusCell },
      { key: 'integration_state', label: 'Integration state', render: bridgeCell },
      {
        key: 'external_status_raw',
        label: 'Raw external status',
        // C17: stored verbatim, never overwritten. An unmapped value is shown
        // as itself and labelled, never replaced with a plausible-looking
        // business status.
        render: (r) => {
          if (!r.external_status_raw) return h('span', { class: 'muted' }, '—');
          const unmapped = r.status_mapped === false || r.mapped_status === null;
          return h('span', { class: 'integration-raw' }, [
            h('span', { class: 'mono' }, String(r.external_status_raw)),
            unmapped
              ? h('span', { class: 'xs' }, [
                text(' '),
                statusChip({
                  label: 'UNMAPPED',
                  tone: 'warning',
                  title: 'This raw value has no mapping in C17_zoho_status_map.json. The record was '
                    + 'accepted, the raw value stored, and an UNMAPPED_EXTERNAL_STATUS '
                    + 'reconciliation exception raised. Nothing was guessed.',
                }),
              ])
              : null,
          ].filter(Boolean));
        },
      },
      { key: 'attempts', label: 'Attempts', numeric: true, render: (r) => text(String(r.attempts ?? '—')) },
      {
        key: 'correlation_id',
        label: 'Correlation id',
        render: (r) => (r.correlation_id
          ? h('a', {
            class: 'linkish integration-link mono',
            href: `?correlation=${encodeURIComponent(r.correlation_id)}#audit-trail`,
            title: 'Open the audit trail for this correlation id.',
          }, String(r.correlation_id))
          : text('—')),
      },
      {
        key: 'message',
        label: 'Detail',
        render: (r) => (r.message ? text(String(r.message)) : h('span', { class: 'muted' }, '—')),
      },
    ],
  });

  const loader = createLoader({
    id: 'eventsStatus',
    glyph: '⇄',
    what: 'The integration event log',
    emptyMessage: 'No integration event matches these filters.',
    loadingMessage: 'Loading integration events…',
    onRetry: () => load(true),
    announce,
  });

  const pagination = createPagination({ id: 'eventsPagination', onMore: () => load(false) });

  root.appendChild(banner);
  root.appendChild(card('eventsTitle', 'Integration Event Monitor', [
    h('p', { class: 'muted small' },
      'Operational statuses (C16) are shown here and only here. An object’s business status is a '
      + 'separate fact on a separate screen; the QUEUED / SENT / FAILED badge in the '
      + '"Integration state" column is the one sanctioned bridge between the two registries.'),
    toolbar, loader.el, table.el, pagination.el,
  ]));

  async function load(reset) {
    if (state.loading) return;
    state.loading = true;
    if (reset) {
      state.rows = []; state.cursor = null; state.hasMore = false;
      table.el.hidden = false;
      table.renderSkeleton();
    }
    pagination.set({ hasMore: state.hasMore, loading: true, shown: state.rows.length });

    await loader.run(() => listEvents({
      direction: directionSelect.value || undefined,
      queue: namespaceSelect.value || undefined,
      module: moduleInput.value.trim() || undefined,
      correlation_id: correlationInput.value.trim() || undefined,
      cursor: reset ? undefined : state.cursor || undefined,
      limit: PAGE_SIZE,
    }), {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items
            : Array.isArray(data && data.events) ? data.events : [];
        state.rows = reset ? items : state.rows.concat(items);
        state.cursor = (data && data.next_cursor) || null;
        state.hasMore = !!(data && data.has_more);
        if (!state.rows.length) {
          // Clear the skeleton rather than hiding it: a hidden stale skeleton
          // still matches a query and still reads as "loading".
          table.renderRows([]); table.el.hidden = true; return false;
        }
        table.el.hidden = false;
        table.renderRows(state.rows);
        announce(`${state.rows.length} integration event${state.rows.length === 1 ? '' : 's'} shown.`);
        return true;
      },
      onState: (s) => { if (s !== 'ready') { table.renderRows([]); table.el.hidden = true; } },
    });

    state.loading = false;
    pagination.set({ hasMore: state.hasMore, loading: false, shown: state.rows.length });
  }

  function renderBanner() {
    while (banner.firstChild) banner.removeChild(banner.firstChild);
    banner.appendChild(modeBanner(state.mode, {
      note: state.modeNote,
      extra: 'In MOCK these events record requests that were constructed and logged, not calls that '
        + 'were made. Response times and record counts are synthesised.',
    }));
  }

  (async () => {
    const globalMode = await getGlobalMode();
    state.mode = globalMode.mode;
    state.modeNote = globalMode.note;
    renderBanner();
    await load(true);
  })();
}
