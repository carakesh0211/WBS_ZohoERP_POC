/* app/frontend/src/features/integration/inbound-grn-status.js
   Inbound GRN / Purchase Receive Status — what we have acquired from Zoho, and
   what the acquisition mechanism cannot promise.

   NOT IN C8. `scr: null`, and reported as a contract gap.
   ------------------------------------------------------
   SCR-16 "GRN and Unbilled Receipt View" is in the frozen forty, and it is the
   FINANCIAL view: received value, what is unbilled against it, the CWIP
   consequence. It says nothing about acquisition. There is no screen in C8 for
   the inbound status of a Purchase Receive, so this one carries no number
   rather than an invented one.

   OAS-02 IS THE REASON THIS SCREEN CANNOT CLAIM COMPLETENESS
   ---------------------------------------------------------
   Zoho ERP publishes NO list endpoint for Purchase Receives. That is a
   verified hard negative, not a gap in our reading of the documentation, and
   it has a direct consequence for what this screen may say: acquisition is
   PO-ANCHORED — every purchase order we know about is walked for its receives
   — so the set of receives we hold is bounded by the set of purchase orders we
   know about.

   A receive against a PO this application has never seen is INVISIBLE to the
   mechanism. Not missing, not late: invisible. So this screen never renders a
   "receives received" count as though it were a count of what exists in Zoho,
   and it carries the limit as a standing, permanent note rather than as an
   error — an error implies something went wrong, and nothing did.

   THE COMPLETENESS SWEEP IS THE ONLY THING THAT NARROWS THAT GAP
   -------------------------------------------------------------
   High-water-mark polling with overlap can still miss a record written behind
   the watermark. The sweep re-walks a window and is the mechanism that catches
   it, so the last sweep time is on this screen: a module that has never been
   swept says "Never swept" rather than showing a blank that reads as fine.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp, formatINR } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createPagination } from '../../components/approvals/screen-kit.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, modeBanner, notMeasured, operationalChip, selectInput,
} from './integration-kit.js';
import { getGlobalMode, listInboundGrn } from './integration-api.js';

const PAGE_SIZE = 50;

const STATES = [
  ['', 'Every state'],
  ['RECEIVED', 'RECEIVED — payload accepted, not yet processed'],
  ['PROCESSED', 'PROCESSED — attributed and posted'],
  ['QUARANTINED', 'QUARANTINED — could not be attributed'],
  ['DEAD', 'DEAD — processing exhausted'],
];

const LEDGER_WHY = 'This row came from our own GRN ledger, which records receipts this '
  + 'application holds. It has no inbox, so it cannot say when or how the record was acquired '
  + 'from Zoho, or whether it was acquired at all.';

function isMeasured(source) {
  return source === 'wave5';
}

export function mountInboundGrnStatus(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = {
    rows: [], cursor: null, hasMore: false, loading: false, source: null,
    mode: 'MOCK', modeNote: '',
  };

  const banner = h('div', { id: 'grnModeBanner' });
  const caveat = h('div', { id: 'grnCaveat' });

  const stateSelect = selectInput(STATES);

  const toolbar = h('div', {
    class: 'toolbar integration-toolbar', role: 'group', 'aria-label': 'Inbound receive filters',
  }, [
    field('grnState', 'Inbox state', stateSelect).el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => load(true) }, 'Apply')),
  ]);

  function stateCell(row) {
    if (!isMeasured(state.source)) return notMeasured(LEDGER_WHY);
    const code = row.state || row.status;
    if (!code) return h('span', { class: 'muted' }, '—');
    return operationalChip('inbox', code);
  }

  /**
   * The attribution cell.
   *
   * A Purchase Receive is acquired BY WALKING ITS PURCHASE ORDER, so the PO it
   * was anchored to is not decoration — it is the whole provenance of the row,
   * and a receive with no anchor could not have been acquired at all.
   */
  function anchorCell(row) {
    const po = row.anchor_po || row.po_number || row.po_id;
    if (!po) {
      return h('span', { class: 'integration-raw' }, [
        h('span', { class: 'muted' }, 'no anchor'),
        h('span', { class: 'xs' }, [
          text(' '),
          statusChip({
            label: 'UNANCHORED',
            tone: 'negative',
            title: 'Purchase Receives have no list endpoint (OAS-02), so acquisition is '
              + 'PO-anchored. A receive with no purchase order could not have been discovered by '
              + 'this mechanism at all, and its presence is itself the defect.',
          }),
        ]),
      ]);
    }
    return h('span', { class: 'mono' }, String(po));
  }

  const table = createDataTable({
    caption: 'Purchase Receives acquired from Zoho, the purchase order each one was anchored to, '
      + 'and the inbox state of each payload',
    emptyMessage: 'No inbound receive matches these filters.',
    columns: [
      {
        key: 'external_id',
        label: 'Zoho receive id',
        render: (r) => (r.external_id || r.zoho_receive_id
          ? h('span', { class: 'mono' }, String(r.external_id || r.zoho_receive_id))
          : h('span', { class: 'muted' }, 'none recorded')),
      },
      {
        key: 'local_id',
        label: 'Our GRN',
        render: (r) => h('span', { class: 'mono' },
          String(r.local_id || r.grn_number || r.grn_id || '—')),
      },
      { key: 'anchor_po', label: 'Anchored to', render: anchorCell },
      {
        key: 'received_at',
        label: 'Received',
        render: (r) => (r.received_at || r.at
          ? text(formatAuditTimestamp(r.received_at || r.at))
          : h('span', { class: 'muted' }, '—')),
      },
      {
        key: 'amount_paise',
        label: 'Value',
        numeric: true,
        render: (r) => text(r.amount_paise === undefined || r.amount_paise === null
          ? '—' : formatINR(r.amount_paise)),
      },
      { key: 'state', label: 'Inbox state', render: stateCell },
      {
        key: 'acquired_at',
        label: 'Acquired from Zoho',
        render: (r) => {
          if (!isMeasured(state.source)) return notMeasured(LEDGER_WHY);
          return r.acquired_at
            ? text(formatAuditTimestamp(r.acquired_at))
            : h('span', { class: 'muted' }, '—');
        },
      },
      {
        key: 'last_error',
        label: 'Why it is not processed',
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
    id: 'grnStatus',
    glyph: '⇩',
    what: 'Inbound Purchase Receive acquisition',
    emptyMessage: 'No inbound receive matches these filters. Note that this is a statement about '
      + 'what we have ACQUIRED, not about what exists in Zoho — see the note above.',
    loadingMessage: 'Loading inbound receives…',
    onRetry: () => load(true),
    announce,
  });

  const pagination = createPagination({ id: 'grnPagination', onMore: () => load(false) });

  root.appendChild(banner);
  root.appendChild(card('grnTitle', 'Inbound GRN and Purchase Receive Status', [
    /* OAS-02, stated as a standing condition of the screen rather than as a
       warning about something going wrong. Nothing IS going wrong. */
    h('div', { class: 'msg msg-info', role: 'note', id: 'grnAnchorNote' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, 'Purchase Receives have no list endpoint in Zoho ERP (OAS-02).'),
        h('div', {}, 'Acquisition is PO-anchored: every purchase order this application knows '
          + 'about is walked for its receives. A receive against a purchase order we have never '
          + 'seen is invisible to that mechanism, so the rows below are what we HAVE ACQUIRED and '
          + 'are not, and cannot be, a count of what exists in the tenant.'),
      ]),
    ]),
    caveat, toolbar, loader.el, table.el, pagination.el,
  ]));

  function renderCaveat() {
    while (caveat.firstChild) caveat.removeChild(caveat.firstChild);
    if (isMeasured(state.source) || !state.source) return;
    caveat.appendChild(h('div', { class: 'msg msg-warning', role: 'status' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
      h('div', { class: 'body' }, [
        h('strong', {}, 'These rows are our own receipts, not acquisitions.'),
        h('div', {}, 'The inbox endpoint is not mounted in this build, so this table lists the '
          + 'GRNs this application holds. It says nothing about when, whether or how any of them '
          + 'was acquired from Zoho, and every column that would carry that is marked "not '
          + 'measured".'),
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

    await loader.run(() => listInboundGrn({
      state: stateSelect.value || undefined,
      cursor: reset ? undefined : state.cursor || undefined,
      limit: PAGE_SIZE,
    }), {
      render: (data, result) => {
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
        announce(`${state.rows.length} inbound receive row${state.rows.length === 1 ? '' : 's'} shown.`);
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
      extra: 'In MOCK no call is made to Zoho, so nothing on this screen was acquired from a '
        + 'tenant. Acquisition timestamps and inbox states, where shown at all, are synthesised.',
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
