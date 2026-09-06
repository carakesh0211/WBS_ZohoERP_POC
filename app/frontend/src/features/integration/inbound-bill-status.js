/* app/frontend/src/features/integration/inbound-bill-status.js
   Inbound Vendor Bill Status — what we have acquired from Zoho, and what the
   raw status on each one actually is.

   NOT IN C8. `scr: null`, and reported as a contract gap.
   ------------------------------------------------------
   SCR-17 "Vendor Bill and Actual CWIP View" is in the frozen forty and is the
   FINANCIAL view of a bill: actualised value, its CWIP consequence, what it
   billed against. It says nothing about acquisition. C8 has no screen for the
   inbound status of a vendor bill, so this one carries no number.

   AN UNMAPPED RAW STATUS IS THE MOST EXPENSIVE MISTAKE AVAILABLE HERE
   ------------------------------------------------------------------
   Zoho's bill statuses are its own vocabulary and this application's are C3's.
   Where a raw value has no mapping in C17_zoho_status_map.json the record is
   still accepted, the RAW VALUE IS STORED VERBATIM, and an
   UNMAPPED_EXTERNAL_STATUS reconciliation exception is raised. Nothing is
   guessed.

   That rule has a visual consequence this screen is responsible for: the raw
   value is shown as itself, labelled UNMAPPED, and NEVER replaced by a
   plausible-looking business status. A bill whose raw status reads
   "partially_billed_weird" rendered as PARTIALLY_ACTUALISED would put a
   fabricated accounting fact in front of a finance user, and it would look
   exactly like a real one.

   ACCOUNTING EFFECT IS A SEPARATE FACT FROM ACQUISITION
   ----------------------------------------------------
   A bill can be acquired perfectly and still have no accounting effect — a
   draft, a void, a reversal. The two are shown as two columns, never merged,
   because "we have it" and "it moves the ledger" are different sentences and
   an operator chasing a missing actual needs to know which of the two failed.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp, formatINR } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createPagination } from '../../components/approvals/screen-kit.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, modeBanner, notMeasured, operationalChip, selectInput, textInput,
} from './integration-kit.js';
import { getGlobalMode, listInboundBills } from './integration-api.js';

const PAGE_SIZE = 50;

const STATES = [
  ['', 'Every state'],
  ['RECEIVED', 'RECEIVED — payload accepted, not yet processed'],
  ['PROCESSED', 'PROCESSED — attributed and posted'],
  ['QUARANTINED', 'QUARANTINED — could not be attributed'],
  ['DEAD', 'DEAD — processing exhausted'],
];

const LEDGER_WHY = 'This row came from our own bill ledger, which records bills this application '
  + 'holds. It has no inbox, so it cannot say when or how the record was acquired from Zoho.';

function isMeasured(source) {
  return source === 'wave5';
}

export function mountInboundBillStatus(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = {
    rows: [], cursor: null, hasMore: false, loading: false, source: null,
    mode: 'MOCK', modeNote: '',
  };

  const banner = h('div', { id: 'billModeBanner' });
  const caveat = h('div', { id: 'billCaveat' });

  const stateSelect = selectInput(STATES);
  const vendorInput = textInput({ maxlength: '80' });

  const toolbar = h('div', {
    class: 'toolbar integration-toolbar', role: 'group', 'aria-label': 'Inbound bill filters',
  }, [
    field('billState', 'Inbox state', stateSelect).el,
    field('billVendor', 'Vendor', vendorInput).el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => load(true) }, 'Apply')),
  ]);

  function stateCell(row) {
    if (!isMeasured(state.source)) return notMeasured(LEDGER_WHY);
    const code = row.state || row.inbox_state;
    if (!code) return h('span', { class: 'muted' }, '—');
    return operationalChip('inbox', code);
  }

  /**
   * The raw external status, verbatim, and labelled where it has no mapping.
   *
   * `status_mapped === false` is the server SAYING it could not map the value.
   * Absence of the flag is not taken as "mapped": on the ledger fallback there
   * is no mapping question at all, and the value shown is our own status,
   * which is labelled as ours.
   */
  function rawStatusCell(row) {
    const raw = row.external_status_raw || (isMeasured(state.source) ? null : row.status);
    if (!raw) return h('span', { class: 'muted' }, '—');
    const unmapped = row.status_mapped === false || row.mapped_status === null;
    return h('span', { class: 'integration-raw' }, [
      h('span', { class: 'mono' }, String(raw)),
      unmapped
        ? h('span', { class: 'xs' }, [
          text(' '),
          statusChip({
            label: 'UNMAPPED',
            tone: 'warning',
            title: 'This raw value has no mapping in C17_zoho_status_map.json. The bill was '
              + 'accepted, the raw value stored verbatim, and an UNMAPPED_EXTERNAL_STATUS '
              + 'reconciliation exception raised. Nothing was guessed and no business status was '
              + 'substituted.',
          }),
        ])
        : null,
      (!isMeasured(state.source))
        ? h('span', { class: 'xs' }, [
          text(' '),
          statusChip({
            label: 'OURS, NOT ZOHO’S',
            tone: 'neutral',
            title: 'The inbox is not mounted in this build, so this is our own bill status, not '
              + 'the raw status Zoho sent. No mapping decision is being shown.',
          }),
        ])
        : null,
    ].filter(Boolean));
  }

  const table = createDataTable({
    caption: 'Vendor bills acquired from Zoho, the raw external status of each one shown verbatim, '
      + 'and whether the bill has any accounting effect',
    emptyMessage: 'No inbound bill matches these filters.',
    columns: [
      {
        key: 'external_id',
        label: 'Zoho bill id',
        render: (r) => (r.external_id || r.zoho_bill_id
          ? h('span', { class: 'mono' }, String(r.external_id || r.zoho_bill_id))
          : h('span', { class: 'muted' }, 'none recorded')),
      },
      {
        key: 'local_id',
        label: 'Our bill',
        render: (r) => h('span', { class: 'mono' },
          String(r.local_id || r.bill_number || r.bill_id || '—')),
      },
      { key: 'vendor_name', label: 'Vendor', render: (r) => text(String(r.vendor_name ?? '—')) },
      {
        key: 'bill_date',
        label: 'Bill date',
        render: (r) => (r.bill_date ? text(formatAuditTimestamp(r.bill_date)) : h('span', { class: 'muted' }, '—')),
      },
      {
        key: 'amount_paise',
        label: 'Value',
        numeric: true,
        render: (r) => text(r.amount_paise === undefined || r.amount_paise === null
          ? '—' : formatINR(r.amount_paise)),
      },
      { key: 'external_status_raw', label: 'Raw status', render: rawStatusCell },
      {
        key: 'accounting_effective',
        label: 'Moves the ledger',
        // A SEPARATE fact from acquisition. A perfectly acquired bill can be a
        // draft, a void or a reversal and move nothing.
        render: (r) => {
          if (r.accounting_effective === undefined || r.accounting_effective === null) {
            return notMeasured('The source that answered did not report whether this document has '
              + 'any accounting effect.');
          }
          return statusChip({
            label: r.accounting_effective ? 'YES' : 'NO',
            tone: r.accounting_effective ? 'positive' : 'neutral',
            title: r.accounting_effective
              ? 'This bill is in an accounting-effective state, so its value is actualised.'
              : 'This bill is acquired but in a state that posts nothing — a draft, a void or a '
                + 'reversal. Acquisition succeeded; the ledger is deliberately unmoved.',
          });
        },
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
    id: 'billStatus',
    glyph: '⇵',
    what: 'Inbound vendor-bill acquisition',
    emptyMessage: 'No inbound bill matches these filters.',
    loadingMessage: 'Loading inbound bills…',
    onRetry: () => load(true),
    announce,
  });

  const pagination = createPagination({ id: 'billPagination', onMore: () => load(false) });

  root.appendChild(banner);
  root.appendChild(card('billTitle', 'Inbound Vendor Bill Status', [
    h('p', { class: 'muted small' },
      'A raw status with no mapping in C17 is shown here exactly as Zoho sent it and labelled '
      + 'UNMAPPED. It is never replaced by a business status: the record is accepted, the raw '
      + 'value stored, and an UNMAPPED_EXTERNAL_STATUS exception raised on the reconciliation '
      + 'exception queue. Acquisition and accounting effect are separate columns because they are '
      + 'separate facts — a bill can be acquired perfectly and still post nothing.'),
    caveat, toolbar, loader.el, table.el, pagination.el,
  ]));

  function renderCaveat() {
    while (caveat.firstChild) caveat.removeChild(caveat.firstChild);
    if (isMeasured(state.source) || !state.source) return;
    caveat.appendChild(h('div', { class: 'msg msg-warning', role: 'status' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
      h('div', { class: 'body' }, [
        h('strong', {}, 'These rows are our own bills, not acquisitions.'),
        h('div', {}, 'The inbox endpoint is not mounted in this build, so this table lists the '
          + 'bills this application holds and the statuses it assigned them. The "Raw status" '
          + 'column is therefore OUR status, marked as such, and no mapping decision is being '
          + 'displayed at all.'),
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

    await loader.run(() => listInboundBills({
      state: stateSelect.value || undefined,
      vendor: vendorInput.value.trim() || undefined,
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
        const unmapped = state.rows.filter((r) => r.status_mapped === false).length;
        announce(`${state.rows.length} inbound bill row${state.rows.length === 1 ? '' : 's'} shown`
          + (unmapped ? `, ${unmapped} with an unmapped raw status.` : '.'));
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
      extra: 'In MOCK no bill on this screen was read from a Zoho tenant. Raw statuses, where they '
        + 'appear at all, are this application’s own.',
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
