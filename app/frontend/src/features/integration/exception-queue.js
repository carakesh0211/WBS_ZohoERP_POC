/* app/frontend/src/features/integration/exception-queue.js
   SCR-27 — Reconciliation Exception Queue: the unmatched documents.

   Named verbatim in research/30_contracts/C8_screens.json as "Reconciliation
   Exception Queue", so this screen carries its real number.

   WHY AN UNMATCHED DOCUMENT BECOMES A ROW HERE INSTEAD OF A NUMBER SOMEWHERE
   -------------------------------------------------------------------------
   An inbound payload that cannot be attributed to a purchase order, a raw
   external status with no mapping in C17, a bill whose value exceeds what was
   ordered — each of these is a fact this application REFUSES to resolve on its
   own. It is never guessed at, never spread pro-rata across the plausible
   lines, and never dropped. It raises a reconciliation_exception, and this
   screen is where a human resolves it.

   That refusal is the control. Pro-rata allocation of an unattributable
   receipt is the single easiest way to produce a CWIP balance that is
   arithmetically perfect and factually invented, and it is invisible
   afterwards because every total still ties.

   THE VARIANCE IS SHOWN AS TWO NUMBERS AND A DIFFERENCE, NEVER AS ONE
   -------------------------------------------------------------------
   `local_paise` and `source_paise` are what WE hold and what the SOURCE said.
   The difference is rendered as its own column, from the server's two values,
   with a sign — negative in parentheses, per C6's financial rule, because
   colour is never the sole carrier of sign. Collapsing them into a single
   "variance" would lose which side is which, and which side is which is the
   first question a person resolving one of these asks.

   AN OPEN EXCEPTION BLOCKS A PERIOD CLOSE
   ---------------------------------------
   `app/backend/pg/periods.py` refuses a CLOSED transition while an entity has
   an Open reconciliation_exception. So this queue is not a diagnostic list: it
   is a hard gate on the accounting calendar, and the screen says so, because
   an operator who does not know that will not understand why the close is
   refused.

   RESOLUTION IS NOT OFFERED FROM THIS BUILD
   -----------------------------------------
   Resolving an exception is a write with an audit consequence, and no endpoint
   for it exists. The screen therefore shows the queue and states plainly that
   resolution is not available here rather than rendering a button that cannot
   work. A disabled button with no explanation is worse than no button: it
   reads as a permission problem.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp, formatINR } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, notMeasured, queryParam, selectInput,
} from './integration-kit.js';
import { listReconciliationExceptions } from './integration-api.js';

const STATUSES = [
  ['', 'Every status'],
  ['Open', 'Open — unresolved, and blocking a period close'],
  ['Resolved', 'Resolved — a correction was made'],
  ['Accepted', 'Accepted — the difference was accepted as final'],
];

/**
 * The exception kinds this screen knows how to explain.
 *
 * An unknown kind is rendered verbatim and NOT given a meaning — the same rule
 * the rest of this feature applies to an unmapped external status. A kind
 * invented by a later stream shows up as itself rather than being quietly
 * filed under the nearest-looking category.
 */
const KINDS = {
  UNMAPPED_EXTERNAL_STATUS: 'A raw status arrived that has no mapping in C17_zoho_status_map.json. '
    + 'The record was accepted and the raw value stored verbatim; no business status was guessed.',
  UNATTRIBUTED_RECEIPT: 'An inbound receipt could not be attributed to a purchase-order line. It '
    + 'was NOT spread pro-rata across the plausible lines, because a pro-rata guess produces a '
    + 'CWIP balance that ties perfectly and is factually invented.',
  OVER_BILLED: 'Billed value exceeds the ordered value of the line. The excess is not absorbed '
    + 'into commitment.',
  VALUE_MISMATCH: 'Our value and the source value differ for the same document. Neither side is '
    + 'overwritten with the other.',
};

const STATUS_TONE = { Open: 'warning', Resolved: 'positive', Accepted: 'neutral' };

export function mountExceptionQueue(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = { rows: [], loading: false, source: null };

  const statusSelect = selectInput(STATUSES, { value: queryParam('status') || '' });
  const tiles = h('div', { id: 'exceptionTiles' });

  const toolbar = h('div', {
    class: 'toolbar integration-toolbar', role: 'group', 'aria-label': 'Exception filters',
  }, [
    field('exceptionStatus', 'Status', statusSelect).el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => load() }, 'Apply')),
  ]);

  /** The difference, from the server's two values, with an explicit sign. */
  function differenceCell(row) {
    const local = row.local_paise;
    const source = row.source_paise;
    if (local === null || local === undefined || source === null || source === undefined) {
      return notMeasured('This exception carries no paired values, so there is no difference to '
        + 'state. That is normal for a kind such as an unmapped status, which is about a code and '
        + 'not about an amount.');
    }
    const diff = Number(local) - Number(source);
    return h('span', {
      title: 'Our value minus the source value. A negative difference is shown in parentheses; '
        + 'colour is never the only carrier of sign.',
    }, formatINR(diff));
  }

  function kindCell(row) {
    const kind = String(row.kind || '').toUpperCase();
    const known = KINDS[kind];
    return h('span', { class: 'mono', title: known
      || `"${row.kind}" is not one of the exception kinds this screen knows how to explain. It is `
        + 'shown as itself rather than filed under the nearest-looking category.' },
    String(row.kind ?? '—'));
  }

  const table = createDataTable({
    caption: 'Reconciliation exceptions: unmatched and unexplained documents, what we hold, what '
      + 'the source said, and the difference between them',
    emptyMessage: 'No reconciliation exception matches this filter.',
    columns: [
      {
        key: 'exception_id',
        label: 'Exception',
        render: (r) => h('span', { class: 'mono' }, String(r.exception_id ?? '—')),
      },
      {
        key: 'raised_at',
        label: 'Raised',
        render: (r) => (r.raised_at ? text(formatAuditTimestamp(r.raised_at)) : h('span', { class: 'muted' }, '—')),
      },
      { key: 'kind', label: 'Kind', render: kindCell },
      {
        key: 'object',
        label: 'Document',
        render: (r) => h('span', { class: 'integration-raw' }, [
          h('span', { class: 'xs muted' }, String(r.object_type ?? '—')),
          text(' '),
          h('span', { class: 'mono' }, String(r.object_id ?? '—')),
        ]),
      },
      {
        key: 'detail',
        label: 'What could not be resolved',
        render: (r) => (r.detail ? text(String(r.detail)) : h('span', { class: 'muted' }, '—')),
      },
      {
        key: 'local_paise',
        label: 'Our value',
        numeric: true,
        render: (r) => (r.local_paise === null || r.local_paise === undefined
          ? h('span', { class: 'muted' }, '—') : text(formatINR(r.local_paise))),
      },
      {
        key: 'source_paise',
        label: 'Source value',
        numeric: true,
        render: (r) => (r.source_paise === null || r.source_paise === undefined
          ? h('span', { class: 'muted' }, '—') : text(formatINR(r.source_paise))),
      },
      { key: 'difference', label: 'Difference', numeric: true, render: differenceCell },
      {
        key: 'status',
        label: 'Status',
        render: (r) => {
          const value = String(r.status || 'Open');
          return statusChip({
            label: value.toUpperCase(),
            tone: STATUS_TONE[value] || 'neutral',
            title: value === 'Open'
              ? 'Unresolved. An entity with an Open reconciliation exception cannot close an '
                + 'accounting period.'
              : value === 'Accepted'
                ? 'The difference was reviewed and accepted as final. Nothing was corrected.'
                : 'A correction was made and the exception no longer stands.',
          });
        },
      },
      {
        key: 'owner_user_id',
        label: 'Owner',
        render: (r) => (r.owner_user_id
          ? h('span', { class: 'mono' }, String(r.owner_user_id))
          : h('span', { class: 'muted' }, 'unassigned')),
      },
      {
        key: 'resolution',
        label: 'Resolution',
        render: (r) => (r.resolution
          ? h('span', {}, [
            text(String(r.resolution)),
            r.resolved_at ? h('div', { class: 'xs muted' }, formatAuditTimestamp(r.resolved_at)) : null,
          ].filter(Boolean))
          : h('span', { class: 'muted' }, '—')),
      },
    ],
  });

  const loader = createLoader({
    id: 'exceptionStatus',
    glyph: '⚑',
    what: 'The reconciliation exception queue',
    emptyMessage: 'No reconciliation exception matches this filter. Nothing is waiting on a human '
      + 'decision, and no exception is blocking a period close.',
    loadingMessage: 'Loading reconciliation exceptions…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(card('exceptionTitle', 'Reconciliation Exception Queue', [
    h('p', { class: 'muted small' },
      'A document that cannot be attributed, a raw status with no mapping, a bill that exceeds its '
      + 'order — none of these is guessed at, spread pro-rata or dropped. Each becomes a row here '
      + 'for a human to decide. An Open exception BLOCKS the close of the accounting period for '
      + 'its entity, so this queue is a gate on the calendar and not a diagnostic list.'),
    /* Stated up front, so nobody hunts for a resolve control that is not
       there. A disabled button would read as a permission problem. */
    h('div', { class: 'msg msg-info', role: 'note', id: 'exceptionResolveNote' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, 'Resolving an exception is not available in this build.'),
        h('div', {}, 'Resolution is a write with an audit consequence and no endpoint for it is '
          + 'mounted. This screen shows the queue; the decision is recorded elsewhere until that '
          + 'route lands. No control is offered here rather than one that cannot work.'),
      ]),
    ]),
    toolbar, loader.el, tiles, table.el,
  ]));

  function renderTiles(rows) {
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
    if (!rows.length) return;
    const open = rows.filter((r) => String(r.status || 'Open') === 'Open');
    const paired = open.filter((r) => r.local_paise !== null && r.local_paise !== undefined
      && r.source_paise !== null && r.source_paise !== undefined);
    const netPaise = paired.reduce((sum, r) => sum + (Number(r.local_paise) - Number(r.source_paise)), 0);
    tiles.appendChild(h('div', { class: 'tiles integration-tiles' }, [
      h('div', { class: open.length ? 'tile accent-watch' : 'tile accent-safe' }, [
        h('div', { class: 'k' }, 'Open exceptions'),
        h('div', { class: 'v' }, String(open.length)),
        h('div', { class: 'sub' }, open.length
          ? 'the accounting period cannot be closed while any of these stands'
          : 'nothing is blocking a period close'),
      ]),
      h('div', { class: 'tile accent-info integration-tile-wide' }, [
        h('div', { class: 'k' }, 'Net difference across open exceptions'),
        h('div', { class: 'v' }, formatINR(netPaise)),
        h('div', { class: 'sub' }, `summed over the ${paired.length} open exception`
          + `${paired.length === 1 ? '' : 's'} that carry a pair of values; `
          + 'the rest are about a code, not an amount'),
      ]),
    ]));
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    table.el.hidden = false;
    table.renderSkeleton();
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);

    await loader.run(() => listReconciliationExceptions({
      status: statusSelect.value || undefined,
    }), {
      render: (data, result) => {
        state.source = result.source;
        const all = Array.isArray(data) ? data
          : Array.isArray(data && data.items) ? data.items
            : Array.isArray(data && data.rows) ? data.rows : [];
        /* The ledger route takes no status filter, so the filter is applied
           here when it answered — and ONLY then, so a Wave 5 route that
           filtered server-side is never double-filtered. */
        const wanted = statusSelect.value;
        const rows = (result.source === 'wave5' || !wanted)
          ? all
          : all.filter((r) => String(r.status || 'Open') === wanted);
        state.rows = rows;
        renderTiles(rows);
        if (!rows.length) { table.renderRows([]); table.el.hidden = true; return false; }
        table.el.hidden = false;
        table.renderRows(rows);
        const open = rows.filter((r) => String(r.status || 'Open') === 'Open').length;
        announce(`${rows.length} reconciliation exception${rows.length === 1 ? '' : 's'} shown, `
          + `${open} open.`);
        return true;
      },
      onState: (s) => {
        if (s !== 'ready') {
          table.renderRows([]);
          table.el.hidden = true;
          while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
        }
      },
    });

    state.loading = false;
  }

  load();
}
