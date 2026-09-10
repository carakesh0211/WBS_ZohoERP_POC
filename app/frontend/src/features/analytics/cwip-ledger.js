/* app/frontend/src/features/analytics/cwip-ledger.js
   SCR-19 — CWIP Ledger.

   Named verbatim in research/30_contracts/C8_screens.json as "CWIP Ledger",
   so this screen carries its real number.

   THE TRAP ON THIS SCREEN, AND WHY THE TOTAL COMES FROM SOMEWHERE ELSE
   --------------------------------------------------------------------
   `/api/bills` returns every bill this caller may see, INCLUDING bills in
   accounting states that are not effective — a voided bill is still a bill and
   is still returned. Actual CWIP is the value of bills that ARE effective:
   `domain.ACCOUNTING_EFFECTIVE_BILL_STATES` decides which, on the server.

   So a "Total actual CWIP" card computed by adding up the rows on this screen
   would overstate CWIP by every voided bill in the list. It would also look
   completely reasonable, which is what makes it dangerous: the arithmetic
   would be correct and the number would be wrong.

   The card therefore comes from `/api/dashboard`'s `totals.actual`, which is
   the server's own figure and already excludes them. The rows come from
   `/api/bills`. The screen states the difference, marks each non-effective row
   as excluded from the total, and the sums-back check runs against the
   EFFECTIVE rows only — so the check is a real check rather than one rigged to
   pass.

   Non-effective bills are still LISTED. Hiding them would remove the evidence
   that a bill was voided from the one screen where a reader is reconciling
   CWIP, and "the row is not there" is a much harder thing to notice than "the
   row is there and marked".
*/

import { h, text } from '../../core/dom.js';
import { createLoader, requireRoot } from './analytics-screen.js';
import {
  card, countCard, createAnnouncer, exportButton, metricCard, reconciliationBlock,
} from './analytics-kit.js';
import {
  createFilterBar, drilldownHref, filterChips, readDrilldownContext, readFilters, screenHref,
  withFilterRemoved, writeFilters,
} from './analytics-filters.js';
import { assertSumsBack, inr, sumColumn } from './analytics-metrics.js';
import { METRIC_DEFINITION, METRIC_LABEL, readRows, readTotals } from './analytics-shapes.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { formatAuditTimestamp } from '../../core/format.js';
import {
  exportAvailable, getCwipLedger, queueExport, SCREENS,
} from './analytics-api.js';

const FILTER_FIELDS = [
  'entity_ids', 'plant_ids', 'project_ids', 'wbs_paths', 'budget_head_ids',
  'category_ids', 'budget_category_ids',
  'vendor_ids', 'date_from', 'date_to', 'period_ids',
];

/** Is this row counted in actual CWIP? The SERVER says so; nothing is inferred. */
function isEffective(row) {
  if (typeof row.accounting_effective === 'boolean') return row.accounting_effective;
  if (typeof row.effective === 'boolean') return row.effective;
  return null;   // the source did not say. Unknown stays unknown.
}

export function mountCwipLedger(root) {
  requireRoot(root, 'SCR-19 CWIP Ledger');
  const announce = createAnnouncer('analyticsLiveRegion');

  let filters = readFilters();
  const context = readDrilldownContext();

  const arrival = h('div', { class: 'analytics-arrival' });
  const chips = h('div', { class: 'analytics-chip-host' });
  const tiles = h('div', { id: 'cwipTiles' });
  const quality = h('div', { class: 'analytics-quality-host' });
  const exportHost = h('span', { class: 'analytics-export-host' });

  const table = createDataTable({
    caption: 'Every vendor bill in scope, with the WBS element and budget head it was posted '
      + 'against, and whether it counts towards actual CWIP',
    emptyMessage: 'No vendor bill matches these filters.',
    columns: [
      {
        key: 'bill_number',
        label: 'Bill',
        render: (r) => h('span', { class: 'mono' }, String(r.bill_number || r.bill_id || '—')),
      },
      {
        key: 'bill_date',
        label: 'Bill date',
        render: (r) => text(r.bill_date ? formatAuditTimestamp(r.bill_date) : '—'),
      },
      {
        key: 'po_number',
        label: 'Purchase order',
        render: (r) => (r.po_number
          ? h('span', { class: 'mono' }, String(r.po_number))
          : h('span', {
            class: 'muted',
            title: 'This bill is not anchored to a purchase order. That is a real state, not a '
              + 'missing value: a bill can be posted without one.',
          }, 'none')),
      },
      {
        key: 'vendor',
        label: 'Vendor',
        render: (r) => text(r.vendor_name || r.vendor_id || '—'),
      },
      {
        key: 'wbs',
        label: 'WBS element(s)',
        render: (r) => {
          const lines = Array.isArray(r.lines) ? r.lines : [];
          const codes = [...new Set(lines.map((l) => l.wbs_code).filter(Boolean))];
          if (r.wbs_code) codes.push(String(r.wbs_code));
          const unique = [...new Set(codes)];
          if (!unique.length) return h('span', { class: 'muted' }, '—');
          return h('span', {}, unique.map((code, i) => h('span', {}, [
            i ? text(', ') : null,
            h('a', {
              class: 'linkish mono',
              href: drilldownHref('analytics-wbs-element', filters, {
                dimension: 'wbs', narrowKey: 'wbs_paths', narrowValue: code,
              }),
            }, code),
          ].filter(Boolean))));
        },
      },
      {
        key: 'accounting_status',
        label: 'Accounting status',
        render: (r) => {
          const effective = isEffective(r);
          const label = String(r.accounting_status || r.status || 'UNKNOWN');
          if (effective === null) {
            return statusChip({
              label,
              tone: 'neutral',
              title: 'The source did not say whether this bill counts towards actual CWIP, so no '
                + 'claim is made either way here.',
            });
          }
          return statusChip({
            label,
            tone: effective ? 'positive' : 'warning',
            title: effective
              ? 'This bill is in an accounting-effective state and IS counted in actual CWIP.'
              : 'This bill is NOT in an accounting-effective state and is EXCLUDED from actual '
                + 'CWIP. It is listed so the exclusion is visible rather than silent.',
          });
        },
      },
      {
        key: 'counted',
        label: 'In CWIP',
        render: (r) => {
          const effective = isEffective(r);
          if (effective === null) {
            return h('span', {
              class: 'analytics-not-reported',
              title: 'Not stated by this source.',
            }, [h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not stated')]);
          }
          return statusChip({
            label: effective ? 'COUNTED' : 'EXCLUDED',
            tone: effective ? 'positive' : 'neutral',
            title: effective
              ? 'Included in the actual CWIP figure above.'
              : 'Excluded from the actual CWIP figure above, by the server, for its accounting '
                + 'state.',
          });
        },
      },
      {
        key: 'amount_paise',
        label: 'Amount',
        numeric: true,
        render: (r) => (r.amount_paise === null || r.amount_paise === undefined
          ? h('span', { class: 'analytics-not-reported' }, [
            h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not reported')])
          : text(inr(r.amount_paise))),
      },
    ],
    renderRowDetail: (r) => {
      const lines = Array.isArray(r.lines) ? r.lines : [];
      if (!lines.length) {
        return h('p', { class: 'xs muted' },
          'This source returned no line detail for this bill. That is not the same as a bill '
          + 'with no lines.');
      }
      const dl = h('dl', { class: 'kv analytics-kv' });
      for (const line of lines) {
        dl.appendChild(h('dt', {}, h('span', { class: 'mono' }, String(line.wbs_code || '—'))));
        dl.appendChild(h('dd', {}, text(
          `${line.budget_head_name || 'no budget head'} — ${inr(line.amount_paise)}`
          + (line.non_creditable_tax_paise
            ? ` plus ${inr(line.non_creditable_tax_paise)} non-creditable tax` : '')
          + (line.freight_paise ? ` plus ${inr(line.freight_paise)} freight` : ''),
        )));
      }
      return dl;
    },
  });

  const filterBar = createFilterBar({
    idPrefix: 'cwip',
    fields: FILTER_FIELDS,
    value: filters,
    onApply: (next) => apply(next),
  });

  const loader = createLoader({
    id: 'cwipStatus',
    glyph: '₹',
    what: 'The CWIP ledger',
    emptyMessage: 'No vendor bill matches these filters. Nothing has been capitalised into work '
      + 'in progress in this slice.',
    loadingMessage: 'Loading the CWIP ledger…',
    onRetry: () => load(),
    announce,
  });

  if (context.metric) {
    arrival.appendChild(h('div', { class: 'msg msg-info', role: 'status' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, `Drill-down from ${METRIC_LABEL[context.metric] || context.metric}.`),
        text(' These are the bills behind that figure, under the same filters.'),
      ]),
    ]));
  }

  root.appendChild(card('cwipTitle', 'CWIP Ledger', [
    arrival,
    h('p', { class: 'muted small' },
      'Actual CWIP is the value of accounting-effective vendor bills. The total below is the '
      + 'server\'s own and already excludes bills that are not effective — a voided bill is still '
      + 'returned by the ledger and is still listed here, marked EXCLUDED, so the exclusion is '
      + 'visible rather than silent. The total is not a sum of the rows on this screen.'),
    filterBar.el,
    chips,
    loader.el,
    tiles,
    quality,
    table.el,
    h('nav', { class: 'analytics-crosslinks', 'aria-label': 'Related views' }, [
      h('span', { class: 'muted' }, 'Related:'),
      h('a', { class: 'linkish', href: screenHref('analytics-cwip-ageing', filters) },
        'CWIP ageing'),
      h('a', { class: 'linkish', href: screenHref('analytics-commitment-ageing', filters) },
        'open commitment ageing'),
      h('a', { class: 'linkish', href: '#integration-reconciliation' },
        'commitment-to-actual reconciliation'),
    ]),
  ], { actions: exportHost }));

  function apply(next) {
    filters = next;
    const query = writeFilters(filters);
    try {
      window.history.replaceState(null, '', `/${query ? `?${query}` : ''}${window.location.hash}`);
    } catch { /* non-navigable context */ }
    load();
  }

  function renderChips(unapplied) {
    while (chips.firstChild) chips.removeChild(chips.firstChild);
    chips.appendChild(filterChips(filters, unapplied,
      (key) => apply(withFilterRemoved(filters, key))));
  }

  function renderCards(totals, rows, effective, excluded) {
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
    while (quality.firstChild) quality.removeChild(quality.firstChild);
    const actual = totals ? (totals.actual ?? totals.actual_paise ?? null) : null;
    const band = h('div', { class: 'tiles analytics-tiles' });

    band.appendChild(metricCard({
      label: METRIC_LABEL.actual,
      paise: actual,
      sub: `${METRIC_DEFINITION.actual} Computed by the server over your whole resolved scope, `
        + 'not summed from the rows below.',
      accent: 'info',
      metric: 'actual',
      href: drilldownHref('analytics-cwip-ageing', filters, { metric: 'actual', dimension: 'period' }),
    }));
    band.appendChild(metricCard({
      label: METRIC_LABEL.received_not_billed,
      paise: totals ? (totals.received_not_billed ?? totals.received_not_billed_paise ?? null) : null,
      sub: METRIC_DEFINITION.received_not_billed,
      accent: 'watch',
      metric: 'received_not_billed',
      href: drilldownHref('analytics-commitment-ageing', filters, {
        metric: 'received_not_billed', dimension: 'vendor',
      }),
    }));
    band.appendChild(countCard({
      label: 'Bills counted',
      count: effective.length,
      sub: 'accounting-effective bills, which are what the total above is made of',
      metric: 'bills_counted',
    }));
    band.appendChild(countCard({
      label: 'Bills excluded',
      count: excluded.length,
      sub: excluded.length
        ? 'listed below and marked EXCLUDED — they are not in the total above'
        : 'no bill in this slice is outside an accounting-effective state',
      accent: excluded.length ? 'watch' : 'safe',
      metric: 'bills_excluded',
    }));
    tiles.appendChild(band);

    /* THE CHECK, RUN AGAINST THE EFFECTIVE ROWS ONLY.
       Running it against every row would be running it against a set the card
       deliberately does not cover, and it would fail on correct data — which
       teaches a reader to ignore the check, which is worse than not having
       one. */
    const check = assertSumsBack(actual, effective, 'amount_paise');
    check.label = `Actual CWIP · card versus the ${effective.length} accounting-effective bill(s) `
      + `below. The ${excluded.length} excluded bill(s) are deliberately outside this check, `
      + 'because the server excludes them from the figure.';
    quality.appendChild(reconciliationBlock(check));

    if (excluded.length) {
      const { total: excludedTotal } = sumColumn(excluded, 'amount_paise');
      quality.appendChild(h('div', { class: 'msg msg-warning', role: 'status' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
        h('div', { class: 'body' }, [
          h('strong', {}, `${excluded.length} bill(s) totalling ${inr(excludedTotal)} are NOT in `
            + 'actual CWIP.'),
          h('div', {}, 'They are listed below and marked EXCLUDED. Adding them to the figure '
            + 'above would overstate CWIP by exactly this amount — which is why the total on this '
            + 'screen is the server\'s and not a sum of what you can see.'),
        ]),
      ]));
    }
  }

  async function wireExport() {
    const availability = await exportAvailable();
    while (exportHost.firstChild) exportHost.removeChild(exportHost.firstChild);
    exportHost.appendChild(exportButton({
      availability,
      report: SCREENS.cwipLedger,
      onExport: async () => {
        const result = await queueExport(SCREENS.cwipLedger, filters);
        announce(result.queued ? 'The export has been queued.'
          : 'This build mounts no export endpoint, so nothing was queued.');
      },
    }));
  }

  let loading = false;

  async function load() {
    if (loading) return;
    loading = true;
    table.el.hidden = false;
    table.renderSkeleton();
    renderChips([]);

    await loader.run(() => getCwipLedger(filters), {
      render: (data, result) => {
        renderChips(result.unapplied);
        const { rows, readable } = readRows(data, ['bills']);
        if (!readable) {
          throw new Error('The CWIP response carried no recognisable row array, so this screen '
            + 'cannot tell an empty ledger from a response it could not read.');
        }
        const totals = readTotals(data);
        /* Partitioned on the SERVER's own flag. A row whose effectiveness the
           source did not state is put with the excluded set for the CHECK —
           counting an unknown as effective would silently widen the basis of a
           figure the server computed narrowly — and is rendered as "not
           stated" rather than as excluded, because those are different facts. */
        const effective = rows.filter((r) => isEffective(r) === true);
        const excluded = rows.filter((r) => isEffective(r) === false);
        renderCards(totals, rows, effective, excluded);
        if (!rows.length) { table.renderRows([]); table.el.hidden = true; return false; }
        // See executive-dashboard.js: `onState('loading')` hides this, and
        // only the success path can put it back.
        table.el.hidden = false;
        table.renderRows(rows);
        announce(`${rows.length} bill(s); ${effective.length} counted in CWIP, `
          + `${excluded.length} excluded.`);
        return true;
      },
      onState: (state) => {
        if (state !== 'ready' && state !== 'stale') {
          table.renderRows([]);
          table.el.hidden = true;
          while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
        }
      },
    });

    loading = false;
  }

  wireExport();
  load();
}
