/* app/frontend/src/features/analytics/executive-dashboard.js
   SCR-01 — Executive CAPEX Dashboard.

   Named verbatim in research/30_contracts/C8_screens.json as "Executive CAPEX
   Dashboard", so this screen carries its real number.

   THIS IS NOT THE SHELL'S `#home` VIEW, AND THE DIFFERENCE IS THE POINT
   ---------------------------------------------------------------------
   app.js's `V.home` renders an executive dashboard today and has approved
   pixel baselines. It is not touched, not repointed and not replaced: it reads
   `/api/dashboard` with two optional filters, renders totals, and is exactly
   what the client approved.

   SCR-01 is the C8 screen that view stands in for, and it does three things
   `V.home` cannot:

     * it carries the CANONICAL FilterSet — nine dimensions, in the URL, shared
       byte for byte with the table, the drill-down and the export;
     * it names its SOURCE and its FRESHNESS, every time, so a reader can tell
       a reporting-service total from a ledger fallback;
     * it CHECKS ITSELF. Every card asserts that its figure equals the sum of
       the rows its drill-down returns, and renders the discrepancy when it
       does not.

   WHAT THIS SCREEN WILL NOT DO
   ----------------------------
   It will not add a number up. Every figure on it is a figure a server sent.
   The one arithmetic performed is `assertSumsBack()`, which compares the
   server's own total against the server's own rows and reports a mismatch —
   the opposite operation, and the only one that can catch a filter applied to
   a card but not to its drill-down.

   It will not show a figure for a metric the answering source did not report.
   "not reported" is rendered instead of ₹0.00, because zero is a measurement
   and no measurement was returned.
*/

import { h, text } from '../../core/dom.js';
import { createLoader, requireRoot } from './analytics-screen.js';
import {
  card, countCard, createAnnouncer, exportButton, metricCard, reconciliationBlock,
} from './analytics-kit.js';
import {
  createFilterBar, drilldownHref, filterChips, readFilters, screenHref, writeFilters,
} from './analytics-filters.js';
import { assertSumsBack } from './analytics-metrics.js';
import {
  METRIC_DEFINITION, METRIC_LABEL, projectRow, readAlerts, readRows, readTotals,
} from './analytics-shapes.js';
import { createProjectTable, METRIC_TARGET, totalsFooter } from './portfolio-table.js';
import {
  exportAvailable, getPortfolio, queueExport, SCREENS,
} from './analytics-api.js';

const SCREEN_ID = 'analytics-executive';

/** The cards, in reading order: what was approved, what is committed, what is spent. */
const CARDS = [
  { key: 'budget', accent: 'info' },
  { key: 'commitment', accent: 'info' },
  { key: 'actual', accent: 'info' },
  { key: 'received_not_billed', accent: 'watch' },
  { key: 'pr_reserved', accent: 'watch' },
  { key: 'available', accent: 'safe' },
];

/** The columns the table carries. Fewer than the ten, and chosen deliberately:
    an executive view that showed all ten would be a spreadsheet, and the four
    buckets that must never be added together are the four that must be
    visible. */
const COLUMNS = ['budget', 'commitment', 'actual', 'received_not_billed', 'available'];

const FILTER_FIELDS = [
  'entity_ids', 'plant_ids', 'location_ids', 'project_ids',
  'budget_head_ids', 'category_ids', 'date_from', 'date_to', 'period_ids',
];

export function mountExecutiveDashboard(root) {
  requireRoot(root, 'SCR-01 Executive CAPEX Dashboard');
  const announce = createAnnouncer('analyticsLiveRegion');

  // THE FILTERS COME FROM THE URL, NOT FROM AN EMPTY OBJECT. That is what
  // makes a pasted link show the sender's figures on a cold load.
  let filters = readFilters();
  const getFilters = () => filters;

  const chips = h('div', { class: 'analytics-chip-host' });
  const tiles = h('div', { id: 'execTiles' });
  const quality = h('div', { class: 'analytics-quality-host' });
  const exportHost = h('span', { class: 'analytics-export-host' });

  const table = createProjectTable({
    metrics: COLUMNS,
    getFilters,
    caption: 'Every CAPEX project your access scope reaches, with its approved budget, open '
      + 'commitment, actual CWIP, unbilled receipts and remaining available budget',
  });

  const filterBar = createFilterBar({
    idPrefix: 'exec',
    fields: FILTER_FIELDS,
    value: filters,
    onApply: (next) => { apply(next); },
  });

  const loader = createLoader({
    id: 'execStatus',
    glyph: '▣',
    what: 'The executive CAPEX portfolio',
    emptyMessage: 'No CAPEX project matches these filters. Widen them, or clear them to see the '
      + 'whole of the portfolio your access scope reaches.',
    loadingMessage: 'Loading the portfolio…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(card('execTitle', 'Executive CAPEX Dashboard', [
    h('p', { class: 'muted small' },
      'Open commitment is ordered less BILLED — a receipt does not release a commitment. '
      + 'Received-not-billed is a separate exposure and the two are never added together. '
      + 'Every figure below is integer paise as the server computed it; nothing on this screen '
      + 'is summed in the browser except the check that the cards and the rows agree.'),
    filterBar.el,
    chips,
    loader.el,
    tiles,
    quality,
    table.el,
    crossLinks(),
  ], { actions: exportHost }));

  /* ---------------- filter application ---------------- */

  /**
   * Apply a FilterSet: push it into the URL, then reload.
   *
   * The URL is written FIRST and with `replaceState`, so the address bar and
   * the figures can never disagree — and so the link a reader copies out of
   * the bar is the link that reproduces what they are looking at. History is
   * replaced rather than pushed because a filter tweak is not a navigation
   * step; twelve back-presses to leave a dashboard is its own defect.
   */
  function apply(next) {
    filters = next;
    const query = writeFilters(filters);
    const url = `/${query ? `?${query}` : ''}${window.location.hash}`;
    try { window.history.replaceState(null, '', url); } catch { /* file:// and the like */ }
    load();
  }

  function renderChips(unapplied) {
    while (chips.firstChild) chips.removeChild(chips.firstChild);
    chips.appendChild(filterChips(filters, unapplied));
  }

  /* ---------------- cards ---------------- */

  function renderCards(totals, rows) {
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
    while (quality.firstChild) quality.removeChild(quality.firstChild);

    const band = h('div', { class: 'tiles analytics-tiles' });
    const checks = [];

    for (const spec of CARDS) {
      const value = totals ? totals[spec.key] : null;
      band.appendChild(metricCard({
        label: METRIC_LABEL[spec.key],
        paise: value === undefined ? null : value,
        sub: METRIC_DEFINITION[spec.key],
        accent: spec.accent,
        metric: spec.key,
        href: drilldownHref(METRIC_TARGET[spec.key] || SCREEN_ID, filters, {
          metric: spec.key, dimension: 'project',
        }),
      }));

      /* THE ROUND TRIP, ASSERTED, ON EVERY CARD.
         The drill-down for these cards is the very table below them — the same
         FilterSet, the same source, one row per project — so the check can be
         run here rather than only after a click. A card that does not equal
         its own rows is a defect whether or not anybody clicked it. */
      const check = assertSumsBack(value, rows, spec.key);
      check.label = `${METRIC_LABEL[spec.key]} · card versus the ${rows.length} project row(s) below`;
      checks.push(check);
    }

    band.appendChild(countCard({
      label: 'Projects in scope',
      count: rows.length,
      sub: 'the rows below, which are what every card above is the sum of',
      metric: 'project_count',
      href: screenHref('analytics-project-list', filters),
    }));

    tiles.appendChild(band);

    /* Every failing check is rendered, and a passing set is summarised in one
       line rather than six. A wall of green ticks trains a reader to skip the
       block, which is exactly where the one red line would have been. */
    const failed = checks.filter((c) => !c.ok);
    if (failed.length) {
      for (const check of failed) quality.appendChild(reconciliationBlock(check));
      announce(`${failed.length} of ${checks.length} figures do not agree with the rows behind them.`);
    } else if (checks.length) {
      quality.appendChild(reconciliationBlock({
        ok: true,
        checked: true,
        message: `All ${checks.length} figures above are exactly the sum of the ${rows.length} `
          + 'project row(s) below them.',
      }));
    }
  }

  /* ---------------- alerts ---------------- */

  function renderAlerts(alerts) {
    if (!alerts.present) return null;
    const items = [];
    const push = (list, kind, describe, msgId) => {
      for (const row of list) items.push(h('div', { class: `msg msg-${kind}` }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, kind === 'error' ? '✖' : '!'),
        h('div', { class: 'body' }, [
          describe(row),
          msgId ? h('div', { class: 'mid' }, msgId) : null,
        ].filter(Boolean)),
      ]));
    };
    push(alerts.budget_exceptions, 'error',
      (r) => h('span', {}, [
        h('strong', {}, String(r.pr_number || r.pr_id || 'A purchase request')),
        text(` — ${r.exception_reason || 'Exceeds available budget.'}`),
      ]), 'MSG-BUD-001');
    push(alerts.over_billed_lines, 'warning',
      (r) => text(`Bill exceeds purchase order on ${r.po_number || '—'} (${r.wbs_code || '—'}).`),
      'MSG-PRC-004');
    return items.length
      ? h('div', {}, items)
      : h('p', { class: 'xs muted' },
        'No budget exception and no over-billed line in this scope. This source DID report the '
        + 'exception lists, so this is a measured "none" rather than an absent answer.');
  }

  /* ---------------- cross-links ---------------- */

  function crossLinks() {
    return h('nav', { class: 'analytics-crosslinks', 'aria-label': 'Related operational views' }, [
      h('span', { class: 'muted' }, 'Operational views:'),
      h('a', { class: 'linkish', href: '#integration-reconciliation' },
        'Commitment-to-actual reconciliation'),
      h('a', { class: 'linkish', href: '#integration-exceptions' },
        'Reconciliation exception queue'),
      h('a', { class: 'linkish', href: '#integration-retry' }, 'Failed sync and retry queue'),
      h('a', { class: 'linkish', href: screenHref('analytics-exceptions', filters) },
        'Exception and overrun monitor'),
    ]);
  }

  /* ---------------- export ---------------- */

  async function wireExport() {
    const availability = await exportAvailable();
    while (exportHost.firstChild) exportHost.removeChild(exportHost.firstChild);
    exportHost.appendChild(exportButton({
      availability,
      report: SCREENS.executive,
      onExport: async () => {
        // The export carries the SAME FilterSet. That is the requirement —
        // "filters cascade through cards, charts, tables AND exports" — and it
        // holds structurally here because there is one object and one
        // serialiser, not because a caller remembered to copy the values.
        const result = await queueExport(SCREENS.executive, filters);
        announce(result.queued
          ? 'The export has been queued. It will appear in your downloads when the job finishes.'
          : 'This build mounts no export endpoint, so nothing was queued.');
      },
    }));
  }

  /* ---------------- load ---------------- */

  let loading = false;

  async function load() {
    if (loading) return;
    loading = true;
    table.el.hidden = false;
    table.renderSkeleton();
    renderChips([]);

    await loader.run(() => getPortfolio(filters), {
      render: (data, result) => {
        renderChips(result.unapplied);
        const { rows: raw, readable } = readRows(data, ['projects']);
        if (!readable) {
          // A shape neither branch recognised. Reported as a failure, because
          // rendering "nothing matched" for it would hide a client/server
          // contract mismatch behind a reassuring sentence.
          throw new Error('The reporting response carried no recognisable row array, so this '
            + 'screen cannot tell an empty portfolio from a response it could not read.');
        }
        const rows = raw.map(projectRow);
        const totals = readTotals(data);
        renderCards(totals, rows);
        const alerts = renderAlerts(readAlerts(data));
        if (alerts) quality.appendChild(alerts);
        if (!rows.length) { table.renderRows([]); table.el.hidden = true; return false; }
        table.renderRows(rows);
        totalsFooter(table, totals, COLUMNS);
        announce(`${rows.length} project${rows.length === 1 ? '' : 's'} in scope.`);
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
