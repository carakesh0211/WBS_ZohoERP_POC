/* app/frontend/src/features/integration/retry-queue.js
   SCR-39 — Failed Sync and Retry Queue (the dead-letter queue), with manual
   retry.

   RETRYING AN OUTBOX ROW CAN CREATE A SECOND PURCHASE ORDER. THAT IS THE WHOLE
   DESIGN PROBLEM ON THIS SCREEN.
   ======================================================================
   WAVE5_CONTRACTS.md's third "most likely to be got wrong": Zoho documents no
   idempotency header. Outbound dedupe is synthesised through a unique custom
   field (`cf_capex_ref`, Z-01), and "a function killed after sending but
   before recording must UPDATE, not duplicate." A DEAD outbox row is, by
   definition, a row where we do not know whether the send landed. So:

     * The idempotency key is minted ONCE per retry attempt and REUSED across
       transport retries of that attempt. A key minted per HTTP call would make
       every browser-level retry a fresh emission — which is precisely the
       duplicate the contract exists to prevent. The key is held per row until
       that row's retry succeeds or is abandoned.
     * A retry of an OUTBOX row asks for confirmation and states, in the
       confirmation, that the previous attempt may already have reached Zoho.
       An INBOX row's retry is a local re-processing and carries no such risk,
       so it does not ask. Treating the two the same would either nag on the
       harmless one or wave through the dangerous one.
     * The button disables itself for the duration of its own request. A
       double-click is the cheapest way to emit two purchase orders and it must
       not be possible.

   A DEAD ROW IS NOT A TERMINAL ROW. C16 marks inbox DEAD, outbox DEAD and job
   DEAD as `terminal: false` — deliberately: "Visible on SCR-39 with manual
   retry." This screen is the mechanism that non-terminality refers to, which
   is why an empty queue here is stated as a positive fact rather than left as
   a blank pane.

   QUARANTINED IS NOT FAILED, AND IS NOT RETRYABLE FROM HERE. A QUARANTINED
   inbox row could not be attributed or mapped; it raised a
   reconciliation_exception and it is NEVER guessed, spread pro-rata or
   dropped. Retrying it would re-run the same failed attribution against the
   same unchanged data. It is listed, with its exception, and pointed at the
   reconciliation exception queue instead.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createPagination } from '../../components/approvals/screen-kit.js';
import { newCorrelationId } from '../../core/api-client.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, modeBanner, operationalChip, queryParam, selectInput,
} from './integration-kit.js';
import { getGlobalMode, listDeadLetters, retryDeadLetter } from './integration-api.js';

const PAGE_SIZE = 50;

const QUEUES = [
  ['', 'Inbox and outbox'],
  ['inbox', 'Inbox — payloads we could not process'],
  ['outbox', 'Outbox — documents we could not emit'],
];

const STATES = [
  ['DEAD', 'DEAD — exhausted every attempt'],
  ['FAILED', 'FAILED — retryable, waiting for backoff'],
  ['QUARANTINED', 'QUARANTINED — could not be attributed'],
  ['', 'Every non-terminal state'],
];

/**
 * The idempotency key for one row's retry ATTEMPT.
 *
 * Held in a module-scoped map keyed by row, minted on first use and kept until
 * that row's retry succeeds. Transport retries of the same attempt reuse it;
 * a fresh attempt after an abandoned one gets a fresh key, because it is a
 * fresh decision by the operator.
 */
const attemptKeys = new Map();

function keyFor(rowKey) {
  if (!attemptKeys.has(rowKey)) attemptKeys.set(rowKey, `retry-${newCorrelationId()}`);
  return attemptKeys.get(rowKey);
}

function rowKeyOf(row) {
  return `${row.queue || row.namespace || 'unknown'}:${row.row_id || row.outbox_id || row.inbox_id || row.id}`;
}

export function mountRetryQueue(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = {
    rows: [], cursor: null, hasMore: false, loading: false, mode: 'MOCK', modeNote: '',
    retrying: new Set(),
  };

  const banner = h('div', { id: 'retryModeBanner' });
  const actionStatus = h('div', { id: 'retryActionStatus', 'aria-live': 'polite' });

  const queueSelect = selectInput(QUEUES, { value: queryParam('queue') || '' });
  const stateSelect = selectInput(STATES);

  const toolbar = h('div', { class: 'toolbar integration-toolbar', role: 'group', 'aria-label': 'Dead-letter filters' }, [
    field('retryQueue', 'Queue', queueSelect).el,
    field('retryState', 'State', stateSelect).el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => load(true) }, 'Apply')),
  ]);

  const table = createDataTable({
    caption: 'Dead-lettered inbox and outbox rows, why each one failed, and the manual retry each one supports',
    emptyMessage: 'Nothing is dead-lettered.',
    columns: [
      {
        key: 'queue',
        label: 'Queue',
        render: (r) => text(String(r.queue || r.namespace || '—')),
      },
      {
        key: 'row_id',
        label: 'Row',
        render: (r) => h('span', { class: 'mono' },
          String(r.row_id || r.outbox_id || r.inbox_id || r.id || '—')),
      },
      { key: 'module', label: 'Module', render: (r) => h('span', { class: 'mono' }, String(r.module ?? '—')) },
      {
        key: 'state',
        label: 'State',
        render: (r) => operationalChip(r.queue || r.namespace || 'outbox', r.state || r.status),
      },
      {
        key: 'local_id',
        label: 'Our document',
        render: (r) => (r.local_id
          ? h('span', { class: 'mono' }, String(r.local_id))
          : h('span', { class: 'muted' }, '—')),
      },
      {
        key: 'external_id',
        label: 'External id',
        // The single most important cell on an outbox row: an external id on a
        // DEAD row means the send DID land and the failure was in recording
        // it. A retry must then update, never create.
        render: (r) => (r.external_id
          ? h('span', { class: 'integration-external' }, [
            h('span', { class: 'mono' }, String(r.external_id)),
            h('span', { class: 'xs' }, [
              text(' '),
              statusChip({
                label: 'ALREADY LANDED',
                tone: 'warning',
                title: 'An external id is recorded, so the document reached Zoho. A retry must '
                  + 'UPDATE by the unique cf_capex_ref, never create a second document.',
              }),
            ]),
          ])
          : h('span', { class: 'muted' }, 'none recorded')),
      },
      { key: 'attempts', label: 'Attempts', numeric: true, render: (r) => text(String(r.attempts ?? '—')) },
      {
        key: 'next_attempt_at',
        label: 'Next automatic attempt',
        render: (r) => (r.next_attempt_at
          ? text(formatAuditTimestamp(r.next_attempt_at))
          : h('span', { class: 'muted' }, 'none — automatic retry is exhausted')),
      },
      {
        key: 'last_error',
        label: 'Why it failed',
        render: (r) => (r.last_error || r.error_message
          ? h('span', {}, [
            r.error_code ? h('span', { class: 'mono xs' }, String(r.error_code)) : null,
            r.error_code ? text(' — ') : null,
            text(String(r.last_error || r.error_message)),
          ].filter(Boolean))
          : h('span', { class: 'muted' }, '—')),
      },
      {
        key: 'correlation_id',
        label: 'Correlation id',
        render: (r) => (r.correlation_id
          ? h('a', {
            class: 'linkish integration-link mono',
            href: `?correlation=${encodeURIComponent(r.correlation_id)}#integration-events`,
            title: 'Open every integration event carrying this correlation id.',
          }, String(r.correlation_id))
          : text('—')),
      },
      { key: 'retry', label: 'Manual retry', render: retryCell },
    ],
  });

  /**
   * The retry control for one row.
   *
   * QUARANTINED rows get an explanation instead of a button: re-running the
   * same failed attribution against the same unchanged data cannot succeed,
   * and offering the action would suggest otherwise.
   */
  function retryCell(row) {
    const stateCode = String(row.state || row.status || '').toUpperCase();
    if (stateCode === 'QUARANTINED') {
      return h('span', { class: 'xs muted' },
        'Not retryable here. The payload could not be attributed; it raised a reconciliation '
        + 'exception and is resolved on the exception queue, never guessed at.');
    }
    const rowKey = rowKeyOf(row);
    const queue = row.queue || row.namespace || 'outbox';
    const rowId = row.row_id || row.outbox_id || row.inbox_id || row.id;
    const button = h('button', {
      type: 'button',
      class: 'btn-sm',
      disabled: !rowId || state.retrying.has(rowKey),
      onClick: () => onRetry(row, button),
    }, state.retrying.has(rowKey) ? 'Retrying…' : 'Retry');
    button.dataset.retryRow = rowKey;
    if (queue === 'outbox') {
      button.title = 'This re-emits a document. The previous attempt may already have reached Zoho, '
        + 'so the retry carries the same idempotency key and updates by cf_capex_ref rather than '
        + 'creating a second document.';
    }
    return button;
  }

  const loader = createLoader({
    id: 'retryStatus',
    glyph: '⇩',
    what: 'The dead-letter queue',
    emptyMessage: 'Nothing is dead-lettered. Every inbox payload was processed and every outbox '
      + 'document was accepted.',
    loadingMessage: 'Loading the dead-letter queue…',
    onRetry: () => load(true),
    announce,
  });

  const pagination = createPagination({ id: 'retryPagination', onMore: () => load(false) });

  root.appendChild(banner);
  root.appendChild(card('retryTitle', 'Failed Sync and Retry Queue', [
    h('p', { class: 'muted small' },
      'DEAD is deliberately not a terminal state: C16 marks it non-terminal precisely because this '
      + 'screen exists. A retry of an outbox row re-emits a document, so it carries one idempotency '
      + 'key per attempt and updates by the unique cf_capex_ref rather than creating a second one — '
      + 'Zoho documents no idempotency header, so that uniqueness is the whole of the protection.'),
    toolbar, actionStatus, loader.el, table.el, pagination.el,
  ]));

  async function onRetry(row, button) {
    const rowKey = rowKeyOf(row);
    const queue = row.queue || row.namespace || 'outbox';
    const rowId = row.row_id || row.outbox_id || row.inbox_id || row.id;
    if (!rowId || state.retrying.has(rowKey)) return;

    if (queue === 'outbox') {
      const landed = row.external_id
        ? `This document already carries external id ${row.external_id}, so the previous attempt `
          + 'DID reach Zoho. The retry will update it by cf_capex_ref, not create a second one.'
        : 'The previous attempt may already have reached Zoho without being recorded here. The '
          + 'retry carries the same idempotency key and updates by cf_capex_ref rather than '
          + 'creating a second document.';
      // eslint-disable-next-line no-alert
      if (!window.confirm(`Re-emit ${rowId}?\n\n${landed}`)) {
        announce('The retry was cancelled.');
        return;
      }
    }

    state.retrying.add(rowKey);
    button.disabled = true;
    button.textContent = 'Retrying…';
    say('info', `Retrying ${rowId}…`);
    try {
      await retryDeadLetter(queue, rowId, keyFor(rowKey));
      // The attempt succeeded, so its key is spent. A later retry of the same
      // row is a NEW decision by the operator and gets a new key.
      attemptKeys.delete(rowKey);
      say('success', `${rowId} was re-queued.`);
      announce(`${rowId} was re-queued.`);
      await load(true);
    } catch (err) {
      if (err && err.name === 'EndpointUnavailableError') {
        say('info', `Manual retry is not available in this build: it does not mount ${err.path}. `
          + 'Nothing was sent.');
      } else {
        // The key is DELIBERATELY KEPT on failure. The next attempt at this
        // same retry must reuse it, or a send that landed but was not recorded
        // becomes a duplicate purchase order.
        say('error', (err && err.message) || `${rowId} could not be re-queued.`);
      }
      announce(`${rowId} was not re-queued.`);
    } finally {
      state.retrying.delete(rowKey);
      button.disabled = false;
      button.textContent = 'Retry';
    }
  }

  function say(kind, message) {
    while (actionStatus.firstChild) actionStatus.removeChild(actionStatus.firstChild);
    actionStatus.appendChild(h('div', {
      class: `msg msg-${kind}`, role: kind === 'error' ? 'alert' : undefined,
    }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, kind === 'error' ? '✖' : kind === 'success' ? '✔' : '·'),
      h('div', { class: 'body' }, message),
    ]));
  }

  async function load(reset) {
    if (state.loading) return;
    state.loading = true;
    if (reset) {
      state.rows = []; state.cursor = null; state.hasMore = false;
      table.el.hidden = false;
      table.renderSkeleton();
    }
    pagination.set({ hasMore: state.hasMore, loading: true, shown: state.rows.length });

    await loader.run(() => listDeadLetters({
      queue: queueSelect.value || undefined,
      state: stateSelect.value || undefined,
      cursor: reset ? undefined : state.cursor || undefined,
      limit: PAGE_SIZE,
    }), {
      render: (data) => {
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items : [];
        state.rows = reset ? items : state.rows.concat(items);
        state.cursor = (data && data.next_cursor) || null;
        state.hasMore = !!(data && data.has_more);
        if (!state.rows.length) { table.renderRows([]); table.el.hidden = true; return false; }
        table.el.hidden = false;
        table.renderRows(state.rows);
        const dead = state.rows.filter((r) => String(r.state || r.status).toUpperCase() === 'DEAD').length;
        announce(`${state.rows.length} row${state.rows.length === 1 ? '' : 's'} in the queue, ${dead} dead.`);
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
      extra: 'In MOCK a retry re-runs the local emission path and records the request it would '
        + 'issue. Nothing is sent to Zoho, so nothing here can duplicate a real document — which '
        + 'will stop being true the moment this connection leaves MOCK.',
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
