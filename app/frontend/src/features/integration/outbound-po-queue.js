/* app/frontend/src/features/integration/outbound-po-queue.js
   The Outbound Purchase Order Queue — what we have tried to emit to Zoho, and
   what became of each attempt.

   NOT IN C8. `scr: null`, and reported as a contract gap.
   ------------------------------------------------------
   research/30_contracts/C8_screens.json's frozen forty has SCR-15 "Purchase
   Order Commitment View", which is the FINANCIAL view of a commitment: ordered
   value, open commitment, what has been received and billed against it. It
   answers nothing about whether the purchase order reached Zoho. There is no
   screen in the registry for the emission queue, so this one carries no SCR
   number rather than an invented SCR-41.

   THE QUESTION THIS SCREEN ASKS IS NOT THE QUESTION THE LEDGER ANSWERS
   -------------------------------------------------------------------
   The outbox is Wave 5's, and `/api/integrations/outbox` is not mounted in
   this build. The honest fallback is this application's own purchase-order
   ledger — which knows every PO that exists and NOTHING about whether any of
   them was emitted, accepted, retried or acknowledged.

   Those are different questions, and the difference is the whole point of the
   screen. So when the ledger answers:

     * every integration column renders `notMeasured()` — not a dash, not
       zero, and not a queued-state chip. A dash, on a table whose other rows
       carry values, reads as "this one has none", which is a claim; zero
       reads as "nothing was queued", which is a claim; and an emission status
       would be a fabricated operational fact, which is the worst of the three.
     * a standing warning above the table says, in words, which question is
       being answered and which one cannot be.
     * the visible source line (integration-kit.js's `sourceLine`) names the
       route by hand.

   IDEMPOTENCY IS THE COLUMN THAT MATTERS ON THIS SCREEN
   ----------------------------------------------------
   Zoho documents no idempotency header. Outbound dedupe is synthesised through
   a unique custom field (`cf_capex_ref`, Z-01): a function killed after
   sending but before recording must UPDATE, not duplicate. So an outbox row
   that carries an external id ALREADY LANDED, whatever its state says, and the
   column says so — the same signal SCR-39 renders for the same reason. An
   operator who re-queues a row without seeing it is one click from a second
   purchase order at the vendor.

   The retry ACTION lives on SCR-39, not here. This screen is a queue view;
   duplicating a write that can emit a document would double the number of
   places that mistake can be made.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp, formatINR } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createPagination } from '../../components/approvals/screen-kit.js';
import { createLoader } from './integration-screen.js';
import {
  INTEGRATION_STATES, createAnnouncer, card, field, integrationStateBadge, modeBanner,
  notMeasured, operationalChip, queryParam, selectInput,
} from './integration-kit.js';
import { getGlobalMode, listOutbox } from './integration-api.js';

const PAGE_SIZE = 50;

/**
 * The filter's state options.
 *
 * THE OUTBOX'S OWN "QUEUED, NOT YET SENT" STATE IS DELIBERATELY ABSENT HERE,
 * AND ITS ABSENCE IS A REPORTED DEFECT IN A TEST GATE, NOT A DESIGN CHOICE.
 *
 * C16 declares that state for the outbox namespace and C15 declares a state of
 * the same NAME for the approval engine. They are different facts in different
 * registries that happen to share a spelling.
 * `tests/test_contracts_integration_statuses.py::
 * test_no_approval_status_is_rendered_outside_the_approval_screens` scans every
 * frontend file for C15 codes as quoted literals and exempts only
 * `features/approvals/**` — unlike its sibling
 * `test_no_integration_status_is_rendered_by_a_business_screen`, which DOES
 * exempt `features/integration/**`. So an integration screen naming the
 * outbox's own state is indistinguishable, to that gate, from a business
 * screen leaking approval state.
 *
 * Three ways out were available and two were rejected:
 *   * editing the gate to carry the same exemption its sibling carries — the
 *     correct fix, and not this stream's file to change;
 *   * splitting the literal so the regex misses it — evading a gate by
 *     obfuscation, which is worse than the thing the gate is guarding;
 *   * dropping the filter shortcut, which is what this does.
 *
 * NOTHING IS HIDDEN FROM THE OPERATOR BY THIS. "Every state" is the default and
 * shows those rows; each one renders its real status through
 * `operationalChip('outbox', …)` from the SERVER's value, which no gate
 * constrains. Only the one-click filter for the least interesting state — the
 * normal, healthy one — is missing. The three states an operator actually comes
 * to this screen for are all here.
 *
 * REPORTED to the lead: the gate needs the integration exemption, after which
 * this option can be restored.
 */
const STATES = [
  ['', 'Every state — including documents still queued and not yet sent'],
  ['SENT', 'SENT — accepted by Zoho'],
  ['FAILED', 'FAILED — retryable, waiting for backoff'],
  ['DEAD', 'DEAD — automatic retry exhausted'],
];

const LEDGER_WHY = 'This row came from our own purchase-order ledger, which records what we '
  + 'ordered. It has no outbox, so it cannot say whether this document was sent to Zoho.';

/**
 * Is this row an OUTBOX row, or a ledger row wearing an outbox row's shape?
 *
 * Decided from the SOURCE that answered, never from the row's fields. A ledger
 * PO that happened to carry a `state` column would otherwise be rendered as
 * though that state were an emission status.
 */
function isMeasured(source) {
  return source === 'wave5';
}

export function mountOutboundPoQueue(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = {
    rows: [], cursor: null, hasMore: false, loading: false, source: null,
    mode: 'MOCK', modeNote: '',
  };

  const banner = h('div', { id: 'outboundModeBanner' });
  const caveat = h('div', { id: 'outboundCaveat' });

  const stateSelect = selectInput(STATES, { value: queryParam('state') || '' });

  const toolbar = h('div', {
    class: 'toolbar integration-toolbar', role: 'group', 'aria-label': 'Outbound queue filters',
  }, [
    field('outboundState', 'Emission state', stateSelect).el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => load(true) }, 'Apply')),
  ]);

  /** An operational status, or an explicit "we did not measure this". */
  function stateCell(row) {
    if (!isMeasured(state.source)) return notMeasured(LEDGER_WHY);
    const code = row.state || row.status;
    if (!code) return h('span', { class: 'muted' }, '—');
    return operationalChip('outbox', code);
  }

  function bridgeCell(row) {
    if (!isMeasured(state.source)) return notMeasured(LEDGER_WHY);
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

  /**
   * The external id, and what it means for a retry.
   *
   * An external id on a row that is not SENT is the single most consequential
   * cell in this table: it means the document DID reach Zoho and the failure
   * was in recording that, so any retry must UPDATE by cf_capex_ref rather
   * than create a second purchase order.
   */
  function externalCell(row) {
    if (!isMeasured(state.source)) return notMeasured(LEDGER_WHY);
    if (!row.external_id) return h('span', { class: 'muted' }, 'none recorded');
    const settled = String(row.state || row.status || '').toUpperCase() === 'SENT';
    return h('span', { class: 'integration-external' }, [
      h('span', { class: 'mono' }, String(row.external_id)),
      settled ? null : h('span', { class: 'xs' }, [
        text(' '),
        statusChip({
          label: 'ALREADY LANDED',
          tone: 'warning',
          title: 'An external id is recorded on a row that is not SENT, so the document reached '
            + 'Zoho and only the recording of it failed. A retry must UPDATE by the unique '
            + 'cf_capex_ref, never create a second document.',
        }),
      ]),
    ].filter(Boolean));
  }

  const table = createDataTable({
    caption: 'Purchase orders queued for emission to Zoho, the state of each attempt, and whether '
      + 'a document already carries an external id',
    emptyMessage: 'Nothing is queued for emission.',
    columns: [
      {
        key: 'local_id',
        label: 'Our purchase order',
        render: (r) => h('span', { class: 'mono' },
          String(r.local_id || r.po_number || r.po_id || r.object_id || '—')),
      },
      {
        key: 'ordered_paise',
        label: 'Ordered value',
        numeric: true,
        render: (r) => text(r.ordered_paise === undefined || r.ordered_paise === null
          ? '—' : formatINR(r.ordered_paise)),
      },
      { key: 'state', label: 'Emission state', render: stateCell },
      { key: 'integration_state', label: 'Integration state', render: bridgeCell },
      { key: 'external_id', label: 'External id', render: externalCell },
      {
        key: 'attempts',
        label: 'Attempts',
        numeric: true,
        render: (r) => (isMeasured(state.source)
          ? text(String(r.attempts ?? '—'))
          : notMeasured(LEDGER_WHY)),
      },
      {
        key: 'next_attempt_at',
        label: 'Next automatic attempt',
        render: (r) => {
          if (!isMeasured(state.source)) return notMeasured(LEDGER_WHY);
          if (r.next_attempt_at) return text(formatAuditTimestamp(r.next_attempt_at));
          return h('span', { class: 'muted' }, 'none scheduled');
        },
      },
      {
        key: 'last_error',
        label: 'Last error',
        render: (r) => {
          if (!isMeasured(state.source)) return notMeasured(LEDGER_WHY);
          if (!r.last_error && !r.error_message) return h('span', { class: 'muted' }, '—');
          return h('span', {}, [
            r.error_code ? h('span', { class: 'mono xs' }, String(r.error_code)) : null,
            r.error_code ? text(' — ') : null,
            text(String(r.last_error || r.error_message)),
          ].filter(Boolean));
        },
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
          : (isMeasured(state.source) ? text('—') : notMeasured(LEDGER_WHY))),
      },
    ],
  });

  const loader = createLoader({
    id: 'outboundStatus',
    glyph: '⇧',
    what: 'The outbound purchase-order queue',
    emptyMessage: 'Nothing is queued for emission. Every purchase order that was due to be sent '
      + 'has been accepted by Zoho.',
    loadingMessage: 'Loading the outbound queue…',
    onRetry: () => load(true),
    announce,
  });

  const pagination = createPagination({ id: 'outboundPagination', onMore: () => load(false) });

  root.appendChild(banner);
  root.appendChild(card('outboundTitle', 'Outbound Purchase Order Queue', [
    h('p', { class: 'muted small' },
      'Zoho documents no idempotency header, so outbound dedupe is synthesised through the unique '
      + 'cf_capex_ref custom field. A row carrying an external id has ALREADY reached Zoho even if '
      + 'its state does not say SENT — the send landed and the recording of it did not — and a '
      + 'retry of such a row must update rather than create. Manual retry lives on the Failed Sync '
      + 'and Retry Queue, not here.'),
    caveat, toolbar, loader.el, table.el, pagination.el,
  ]));

  /**
   * The standing warning shown whenever the ledger answered instead of the
   * outbox. It is a `.msg-warning` with an `!` glyph and a full sentence: the
   * `--warning` token alone fails contrast, so the meaning is carried by the
   * glyph and the words, never by the colour.
   */
  function renderCaveat() {
    while (caveat.firstChild) caveat.removeChild(caveat.firstChild);
    if (isMeasured(state.source) || !state.source) return;
    caveat.appendChild(h('div', { class: 'msg msg-warning', role: 'status' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
      h('div', { class: 'body' }, [
        h('strong', {}, 'These rows are purchase orders, not emissions.'),
        h('div', {}, 'The outbox endpoint is not mounted in this build, so this table lists what '
          + 'this application HOLDS, not what it SENT. Nothing here says whether any of these '
          + 'documents reached Zoho, and every column that would carry that is marked "not '
          + 'measured" rather than filled in with a plausible value.'),
      ]),
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

    await loader.run(() => listOutbox({
      state: stateSelect.value || undefined,
      module: 'purchase-order',
      cursor: reset ? undefined : state.cursor || undefined,
      limit: PAGE_SIZE,
    }), {
      render: (data, result) => {
        // The source is set BEFORE any cell renders: every renderer above asks
        // it whether the row it has can be described as an emission at all.
        state.source = result.source;
        renderCaveat();
        const items = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items
            : Array.isArray(data && data.rows) ? data.rows : [];
        state.rows = reset ? items : state.rows.concat(items);
        state.cursor = (data && data.next_cursor) || null;
        state.hasMore = !!(data && data.has_more);
        if (!state.rows.length) { table.renderRows([]); table.el.hidden = true; return false; }
        table.el.hidden = false;
        table.renderRows(state.rows);
        announce(isMeasured(state.source)
          ? `${state.rows.length} outbound row${state.rows.length === 1 ? '' : 's'} shown.`
          : `${state.rows.length} purchase order${state.rows.length === 1 ? '' : 's'} shown from `
            + 'the local ledger. Emission state is not available in this build.');
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
      extra: 'In MOCK an emission constructs and logs the request it would issue and never sends '
        + 'it, so no row here corresponds to a document that exists in a Zoho tenant.',
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
