/* app/frontend/src/features/integration/reconciliation-workbench.js
   SCR-18 — Commitment-to-Actual Reconciliation.

   Named verbatim in research/30_contracts/C8_screens.json as
   "Commitment-to-Actual Reconciliation", so this screen carries its real
   number.

   THE ONE ARITHMETIC THIS SCREEN MUST NOT GET WRONG
   -------------------------------------------------
   Open commitment is `ordered − billed`, floored at zero, and ZERO OUTRIGHT
   once the purchase order has reached a commitment-releasing state. It is NOT
   `ordered − received`. A receipt does not release a commitment; a bill does.
   Getting that backwards understates open commitment on every partially
   received line in the estimate, which is the number the whole CAPEX control
   exists to protect.

   Received-not-billed is a SEPARATE quantity, `received − billed` floored at
   zero, and it is the unbilled-receipt exposure — value that has arrived and
   has not yet been invoiced. It overlaps with open commitment by design and
   the two are never added together.

   Exposure is `open commitment + billed`: what this line can still cost plus
   what it already has. It is shown as its own column rather than left for the
   reader to add, because a reader adding the wrong two columns is exactly the
   mistake above.

   MONEY IS INTEGER PAISE, END TO END
   ----------------------------------
   Every value on this screen is integer paise from the server and goes through
   `formatINR()`, which renders Indian digit grouping, two decimals, and a
   NEGATIVE IN PARENTHESES — colour is never the sole carrier of sign
   (C6_tokens.json's financial rule). No arithmetic is done in this file: a
   float rupee anywhere in the chain is a rounding error in a financial
   control, and the totals shown are the server's own.

   THE FLAG IS NOT A STATUS
   ------------------------
   `flag` here is a reconciliation POSITION — ok, released, received-unbilled,
   over-billed — computed from the arithmetic above. It is deliberately not
   rendered as a C3 business status and not as a C16 operational one; it is its
   own vocabulary, with the plain-language `position` sentence beside it so
   nobody has to decode it.
*/

import { h, text } from '../../core/dom.js';
import { formatINR } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { createLoader } from './integration-screen.js';
import {
  createAnnouncer, card, field, notMeasured, queryParam, textInput,
} from './integration-kit.js';
import { getReconciliation } from './integration-api.js';

/**
 * The reconciliation flags, their tone, and — the part that matters — a
 * GLYPH and a full-word label, so the meaning survives greyscale.
 *
 * The `--warning` token used to fail WCAG AA 4.5:1 as text on every background
 * in the frozen stylesheet; the Wave 8 approved change moved warning TEXT to
 * the darker amber and closed that. Nothing here ever relied on colour and
 * nothing here relies on it now: every flag carries a symbol from the chip
 * component plus its own word.
 */
const FLAGS = {
  'over-billed': {
    tone: 'negative',
    label: 'OVER-BILLED',
    title: 'Billed value exceeds the ordered value of this line. The excess is not a commitment '
      + 'and is not absorbed silently: it raises a reconciliation exception.',
  },
  'received-unbilled': {
    tone: 'warning',
    label: 'RECEIVED, NOT BILLED',
    title: 'Value has been received against this line and not yet invoiced. This is unbilled '
      + 'receipt exposure; it is NOT the same quantity as open commitment and the two are never '
      + 'added together.',
  },
  released: {
    tone: 'neutral',
    label: 'RELEASED',
    title: 'The purchase order has reached a commitment-releasing state, so open commitment on '
      + 'this line is zero regardless of what remains unbilled.',
  },
  ok: {
    tone: 'positive',
    label: 'IN LINE',
    title: 'Ordered, received and billed values reconcile with no exception on this line.',
  },
};

function flagCell(row) {
  const key = String(row.flag || 'ok');
  const spec = FLAGS[key];
  if (!spec) {
    // An unknown flag is shown verbatim and never given a meaning. Same rule
    // as an unmapped external status: no guess is better than a wrong guess.
    return statusChip({
      label: key.toUpperCase(),
      tone: 'neutral',
      title: `"${key}" is not one of the reconciliation flags this screen knows. It is shown as `
        + 'itself rather than interpreted.',
    });
  }
  return statusChip({ label: spec.label, tone: spec.tone, title: spec.title });
}

export function mountReconciliationWorkbench(root) {
  if (!root) return;
  const announce = createAnnouncer('integrationLiveRegion');

  const state = { rows: [], summary: null, loading: false };

  const tiles = h('div', { id: 'reconTiles' });
  const projectInput = textInput({ maxlength: '40', value: queryParam('project') || '' });

  const toolbar = h('div', {
    class: 'toolbar integration-toolbar', role: 'group', 'aria-label': 'Reconciliation filters',
  }, [
    field('reconProject', 'Project', projectInput,
      { hint: 'Leave blank to reconcile every project this principal may see.' }).el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => load() }, 'Apply')),
  ]);

  const money = (key) => ({
    numeric: true,
    render: (r) => text(formatINR(r[key])),
  });

  const table = createDataTable({
    caption: 'Every purchase-order line, with what was ordered, received and billed against it, '
      + 'the open commitment that remains, and the reconciliation position of the line',
    emptyMessage: 'No purchase-order line matches this filter.',
    columns: [
      {
        key: 'po_number',
        label: 'Purchase order',
        render: (r) => h('span', { class: 'mono' }, `${r.po_number ?? '—'}/${r.line_no ?? '—'}`),
      },
      { key: 'wbs_code', label: 'WBS', render: (r) => h('span', { class: 'mono' }, String(r.wbs_code ?? '—')) },
      { key: 'vendor_name', label: 'Vendor', render: (r) => text(String(r.vendor_name ?? '—')) },
      { key: 'ordered_paise', label: 'Ordered', ...money('ordered_paise') },
      { key: 'received_paise', label: 'Received', ...money('received_paise') },
      { key: 'billed_paise', label: 'Billed', ...money('billed_paise') },
      {
        key: 'open_commitment_paise',
        label: 'Open commitment',
        numeric: true,
        // ordered − billed, floored at zero, and zero outright once released.
        // NOT ordered − received.
        render: (r) => h('span', {
          title: 'Ordered minus billed, floored at zero — and zero outright once the purchase '
            + 'order is in a commitment-releasing state. A receipt does not release a commitment; '
            + 'a bill does.',
        }, formatINR(r.open_commitment_paise)),
      },
      {
        key: 'received_not_billed_paise',
        label: 'Received, not billed',
        numeric: true,
        render: (r) => h('span', {
          title: 'Received minus billed, floored at zero. This is unbilled-receipt exposure. It '
            + 'overlaps open commitment by design and the two are never added together.',
        }, formatINR(r.received_not_billed_paise)),
      },
      {
        key: 'exposure_paise',
        label: 'Exposure',
        numeric: true,
        render: (r) => h('span', {
          title: 'Open commitment plus billed: what this line can still cost plus what it already '
            + 'has. Shown as a column rather than left to be added, because adding the wrong two '
            + 'columns is the mistake this screen exists to prevent.',
        }, formatINR(r.exposure_paise)),
      },
      {
        key: 'emission_state',
        label: 'Reached Zoho',
        // THE HALF THE LEDGER FALLBACK CANNOT ANSWER, and the only reason the
        // Wave 5 route exists at all. `/api/reconciliation` knows what we
        // ordered; it does not know whether the order was ever emitted.
        //
        // Three distinct renderings, and collapsing any two of them would be
        // the defect: a NAMED state when there is an outbox row, "not
        // measured" when the response could not answer (the ledger fallback,
        // which carries no such field), and NEVER a dash standing in for
        // either. A dash reads as "no", and "we did not look" is not "no".
        render: (r) => {
          if (!('emission_state' in r)) {
            return notMeasured('This reconciliation came from the local ledger, which records '
              + 'what was ordered and holds no record of what was emitted. Nothing here was '
              + 'measured against the outbox.');
          }
          const emission = r.emission_state;
          if (!emission || !emission.rows) {
            return h('span', {
              class: 'xs muted',
              title: 'There is no outbox row for this purchase order at all: nothing has ever '
                + 'been enqueued for it. That is a real answer, not a missing one.',
            }, 'never enqueued');
          }
          // ONE PURCHASE ORDER CAN BE SEVERAL EMISSIONS. On a product whose
          // custom fields are header-only, a multi-cell order is SPLIT — one
          // emission per control cell — so its rows can legitimately disagree.
          // The server sends a single `state` only when they all agree; when
          // they do not it sends null and a breakdown, and this renders the
          // breakdown rather than picking a winner. Picking the worst would
          // hide a success and picking the first would hide a failure.
          if (!emission.state) {
            const breakdown = Object.entries(emission.states || {})
              .map(([s, n]) => `${n}×${s}`).join(', ');
            return h('span', {
              class: 'mono integration-raw',
              title: `This purchase order was emitted as ${emission.rows} outbox rows and they do `
                + 'not agree. It was split across control cells because this product carries its '
                + 'custom fields on the header only, so each cell went as its own document.',
            }, breakdown || `${emission.rows} rows`);
          }
          return h('span', {
            class: 'mono integration-external',
            title: emission.external_id
              ? `Accepted as ${emission.external_id}.`
              : emission.split
                ? `Split across ${emission.rows} control cells; every one is ${emission.state}.`
                : 'Enqueued, and not yet accepted: no external id has come back.',
          }, emission.split
            ? `${emission.state} (${emission.rows})`
            : String(emission.state));
        },
      },
      { key: 'flag', label: 'Position', render: flagCell },
      {
        key: 'position',
        label: 'In words',
        render: (r) => (r.position ? text(String(r.position)) : h('span', { class: 'muted' }, '—')),
      },
    ],
  });

  const loader = createLoader({
    id: 'reconStatus',
    glyph: '⚖',
    what: 'The commitment-to-actual reconciliation',
    emptyMessage: 'No purchase-order line matches this filter, so there is nothing to reconcile.',
    loadingMessage: 'Reconciling commitments against actuals…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(card('reconTitle', 'Commitment-to-Actual Reconciliation', [
    h('p', { class: 'muted small' },
      'Open commitment is ordered minus BILLED, floored at zero, and zero outright once the '
      + 'purchase order is released — a receipt does not release a commitment. Received-not-billed '
      + 'is a separate quantity and the two are never added. Every figure is integer paise from '
      + 'the server; nothing on this screen is computed in the browser.'),
    toolbar, loader.el, tiles, table.el,
  ]));

  /**
   * The control totals band.
   *
   * These are the SERVER's sums, rendered, never re-derived here from the rows
   * on screen: a total computed from a paginated page would silently be the
   * total of that page, which on a reconciliation screen is a number an
   * operator would sign off.
   */
  /**
   * DID THE FOUR QUANTITIES ACTUALLY RECONCILE?
   *
   * The server states the identity it holds itself to —
   * `ordered - billed = open commitment + residual released - over-billed` —
   * and reports the residual in paise. This renders that answer rather than
   * letting the reader assume it, because the whole screen is a claim that
   * these numbers tie out, and a claim nobody checks is a claim nobody can
   * trust.
   *
   * A NON-ZERO RESIDUAL IS SHOWN, NOT SWALLOWED. Money has either been counted
   * twice or lost, and which is worse depends on the sign; either way an
   * operator about to sign the total off is entitled to know the parts did not
   * add up. It is rendered as an ALERT, in words, never colour alone.
   *
   * A source that does not state an identity — the ledger fallback — gets
   * "not measured" rather than a green tick. The ledger's arithmetic is the
   * same arithmetic; it simply does not report the check, and reporting a
   * check that was never run would be the one dishonesty this screen cannot
   * afford.
   */
  function identityTile(summary) {
    if (!summary || !summary.identity) {
      return h('div', { class: 'tile accent-info' }, [
        h('div', { class: 'k' }, 'Reconciles to the paisa'),
        h('div', { class: 'v' }, notMeasured('This source does not state a reconciliation '
          + 'identity, so no check was run. The figures are not asserted to tie out here.')),
        h('div', { class: 'sub' }, 'the ledger source reports no identity check'),
      ]);
    }
    const balanced = summary.identity_balanced === true;
    const residual = Number(summary.identity_residual_paise || 0);
    return h('div', {
      class: `tile ${balanced ? 'accent-info' : 'accent-watch'}`,
      role: balanced ? undefined : 'alert',
    }, [
      h('div', { class: 'k' }, 'Reconciles to the paisa'),
      h('div', { class: 'v' }, [
        h('span', { class: 'sym', 'aria-hidden': 'true' }, balanced ? '\u2714' : '\u2716'),
        text(' '),
        text(balanced ? 'BALANCED' : 'DOES NOT BALANCE'),
      ]),
      h('div', { class: 'sub' }, balanced
        ? String(summary.identity)
        : `${summary.identity} — off by ${formatINR(residual)}. Money has been counted twice or `
          + 'lost; these totals must not be signed off until it is found.'),
    ]);
  }

  function renderTiles(summary) {
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
    if (!summary) return;
    const exceptions = Array.isArray(summary.exceptions) ? summary.exceptions.length
      : Number(summary.exceptions || 0);
    tiles.appendChild(h('div', { class: 'tiles integration-tiles' }, [
      h('div', { class: 'tile accent-info' }, [
        h('div', { class: 'k' }, 'Lines reconciled'),
        h('div', { class: 'v' }, String(summary.lines ?? '—')),
        h('div', { class: 'sub' }, 'server-side count, not a count of the rows on screen'),
      ]),
      h('div', { class: 'tile accent-info integration-tile-wide' }, [
        h('div', { class: 'k' }, 'Open commitment'),
        h('div', { class: 'v' }, formatINR(summary.open_commitment_paise)),
        h('div', { class: 'sub' }, 'ordered less billed, released purchase orders excluded'),
      ]),
      h('div', { class: 'tile accent-info integration-tile-wide' }, [
        h('div', { class: 'k' }, 'Billed'),
        h('div', { class: 'v' }, formatINR(summary.billed_paise)),
        h('div', { class: 'sub' }, 'actualised value against these lines'),
      ]),
      h('div', { class: 'tile accent-info integration-tile-wide' }, [
        h('div', { class: 'k' }, 'Received, not billed'),
        h('div', { class: 'v' }, formatINR(summary.received_not_billed_paise)),
        h('div', { class: 'sub' }, 'unbilled receipt exposure — NOT part of open commitment'),
      ]),
      identityTile(summary),
      h('div', { class: 'tile accent-watch' }, [
        h('div', { class: 'k' }, 'Lines raising an exception'),
        h('div', { class: 'v' }, String(exceptions)),
        h('div', { class: 'sub' }, [
          text('over-billed or received-unbilled — '),
          h('a', { class: 'linkish', href: '#integration-exceptions' }, 'open the exception queue'),
        ]),
      ]),
    ]));
  }

  async function load() {
    if (state.loading) return;
    state.loading = true;
    table.el.hidden = false;
    table.renderSkeleton();
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);

    await loader.run(() => getReconciliation(projectInput.value.trim() || undefined), {
      render: (data) => {
        const rows = Array.isArray(data) ? data
          : Array.isArray(data && data.rows) ? data.rows
            : Array.isArray(data && data.items) ? data.items : [];
        state.rows = rows;
        state.summary = (data && data.summary) || null;
        renderTiles(state.summary);
        if (!rows.length) { table.renderRows([]); table.el.hidden = true; return false; }
        table.el.hidden = false;
        table.renderRows(rows);
        const flagged = rows.filter((r) => r.flag === 'over-billed' || r.flag === 'received-unbilled').length;
        announce(`${rows.length} line${rows.length === 1 ? '' : 's'} reconciled, `
          + `${flagged} raising an exception.`);
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
