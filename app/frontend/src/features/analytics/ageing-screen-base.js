/* app/frontend/src/features/analytics/ageing-screen-base.js
   The shell SCR-23 and SCR-24 share.

   THE HONEST SHAPE OF AN AGEING SCREEN IN THIS BUILD
   ---------------------------------------------------
   An ageing table is a GROUPED AGGREGATE — value by bucket, over the caller's
   whole resolved scope. Nothing mounted in this build produces one.
   `/api/purchase-orders` and `/api/bills` return a page of documents, and
   bucketing that page in the browser would produce a table whose totals are
   the totals of one page under headings claiming to be the totals of a
   portfolio. That is the exact failure the Wave 7 contract names first, so the
   buckets stay UNAVAILABLE, by route name, until A1 mounts the report.

   The TOTAL is a different matter. `/api/reconciliation` computes open
   commitment over the caller's scope and reports it in its own summary;
   `/api/dashboard` does the same for actual CWIP. Those are measured,
   server-side figures and they are shown.

   So this screen carries two independent loaders and two independent states,
   and that is the point rather than an accident of implementation:

       the total     — READY, from a named source, with its freshness
       the buckets   — UNAVAILABLE, naming /api/reports/<report-id>

   Collapsing them would force a choice between two dishonest screens: one that
   showed nothing at all when a real figure was available, and one that
   presented a browser-side bucketing as though a server had computed it. Two
   loaders is what it costs to say two different true things at once.

   A DISTRIBUTION IS NOT DECORATION ON A TOTAL
   -------------------------------------------
   It matters that the screen does not imply otherwise. The total card is not
   labelled "total" over an empty bucket strip, as though the strip were merely
   waiting to be filled with a breakdown of that number — the unavailable block
   sits where the buckets would be and says what is missing and why nothing is
   shown there.
*/

import { h, text } from '../../core/dom.js';
import { createLoader, requireRoot } from './analytics-screen.js';
import {
  card, createAnnouncer, exportButton, metricCard, reconciliationBlock,
} from './analytics-kit.js';
import {
  createFilterBar, drilldownHref, filterChips, readFilters, screenHref, writeFilters,
} from './analytics-filters.js';
import { assertSumsBack, inr } from './analytics-metrics.js';
import { METRIC_DEFINITION, METRIC_LABEL, readRows } from './analytics-shapes.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { exportAvailable, getAgeing, queueExport } from './analytics-api.js';

const FILTER_FIELDS = [
  'entity_ids', 'plant_ids', 'project_ids', 'wbs_paths', 'budget_head_ids',
  'category_ids', 'vendor_ids', 'date_from', 'date_to', 'period_ids',
];

/**
 * @param {Object} spec
 * @param {string} spec.screen
 * @param {string} spec.kind - 'commitment' | 'cwip'.
 * @param {string} spec.idPrefix
 * @param {string} spec.title
 * @param {string} spec.intro
 * @param {string} spec.reportId
 * @param {Function} spec.fetchTotal - the api call for the measured total.
 * @param {Array<{key:string,label:string,sub:string,accent:string}>} spec.totals
 * @param {string} spec.rowMetric - the money key on a drill-down row.
 * @param {string} spec.bucketWhat - "Open commitment ageing".
 */
export function createAgeingScreen(spec) {
  return function mount(root) {
    requireRoot(root, spec.screen);
    const announce = createAnnouncer('analyticsLiveRegion');

    let filters = readFilters();

    const chips = h('div', { class: 'analytics-chip-host' });
    const tiles = h('div', { id: `${spec.idPrefix}Tiles` });
    const quality = h('div', { class: 'analytics-quality-host' });
    const exportHost = h('span', { class: 'analytics-export-host' });

    const buckets = createDataTable({
      caption: `${spec.bucketWhat} by ageing bucket, with the value in each`,
      emptyMessage: 'The report returned no ageing bucket.',
      columns: [
        { key: 'bucket', label: 'Ageing bucket', render: (r) => text(String(r.bucket ?? r.label ?? '—')) },
        {
          key: 'count',
          label: 'Documents',
          numeric: true,
          render: (r) => text(r.count === null || r.count === undefined ? '—' : String(r.count)),
        },
        {
          key: 'amount_paise',
          label: 'Value',
          numeric: true,
          render: (r) => (r.amount_paise === null || r.amount_paise === undefined
            ? h('span', { class: 'analytics-not-reported' }, [
              h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not reported')])
            : h('a', {
              class: 'linkish analytics-drill',
              href: drilldownHref(
                spec.kind === 'cwip' ? 'analytics-cwip-ledger' : 'analytics-commitment-ageing',
                filters,
                { metric: spec.rowMetric, dimension: 'period' },
              ),
            }, inr(r.amount_paise))),
        },
      ],
    });

    const totalLoader = createLoader({
      id: `${spec.idPrefix}TotalStatus`,
      glyph: '₹',
      what: `The ${spec.kind === 'cwip' ? 'actual CWIP' : 'open commitment'} total`,
      emptyMessage: `Nothing is ${spec.kind === 'cwip' ? 'capitalised' : 'committed'} in this `
        + 'slice, so there is nothing to age.',
      loadingMessage: 'Loading the measured total…',
      onRetry: () => loadTotal(),
      announce,
    });

    const bucketLoader = createLoader({
      id: `${spec.idPrefix}BucketStatus`,
      glyph: '◷',
      what: spec.bucketWhat,
      emptyMessage: 'The report returned no ageing bucket for these filters.',
      loadingMessage: 'Loading the ageing distribution…',
      onRetry: () => loadBuckets(),
      announce,
    });

    const filterBar = createFilterBar({
      idPrefix: spec.idPrefix,
      fields: FILTER_FIELDS,
      value: filters,
      onApply: (next) => apply(next),
    });

    root.appendChild(card(`${spec.idPrefix}Title`, spec.title, [
      h('p', { class: 'muted small' }, spec.intro),
      filterBar.el,
      chips,
      h('h3', { class: 'analytics-subhead' }, 'Measured total'),
      totalLoader.el,
      tiles,
      quality,
      h('h3', { class: 'analytics-subhead' }, 'Ageing distribution'),
      bucketLoader.el,
      buckets.el,
      h('nav', { class: 'analytics-crosslinks', 'aria-label': 'Related views' }, [
        h('span', { class: 'muted' }, 'Related:'),
        h('a', { class: 'linkish', href: screenHref('analytics-cwip-ledger', filters) },
          'the CWIP ledger'),
        h('a', { class: 'linkish', href: screenHref('analytics-exceptions', filters) },
          'the exception monitor'),
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
      loadTotal();
      loadBuckets();
    }

    function renderChips(unapplied) {
      while (chips.firstChild) chips.removeChild(chips.firstChild);
      chips.appendChild(filterChips(filters, unapplied));
    }

    function renderCards(summary, rows) {
      while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
      while (quality.firstChild) quality.removeChild(quality.firstChild);
      const band = h('div', { class: 'tiles analytics-tiles' });
      for (const t of spec.totals) {
        const value = summary ? (summary[t.key] ?? summary[`${t.key}_paise`] ?? null) : null;
        band.appendChild(metricCard({
          label: t.label,
          paise: value,
          sub: t.sub,
          accent: t.accent,
          metric: t.key,
          href: drilldownHref(t.target || 'analytics-cwip-ledger', filters, {
            metric: t.key, dimension: 'vendor',
          }),
        }));
      }
      tiles.appendChild(band);

      if (rows && rows.length) {
        const primary = spec.totals[0];
        const value = summary
          ? (summary[primary.key] ?? summary[`${primary.key}_paise`] ?? null) : null;
        const check = assertSumsBack(value, rows, spec.rowMetric);
        check.label = `${primary.label} · total versus the ${rows.length} line(s) the same source `
          + 'returned';
        quality.appendChild(reconciliationBlock(check));
      }
    }

    async function wireExport() {
      const availability = await exportAvailable();
      while (exportHost.firstChild) exportHost.removeChild(exportHost.firstChild);
      exportHost.appendChild(exportButton({
        availability,
        reportId: spec.reportId,
        onExport: async () => {
          const result = await queueExport(spec.reportId, filters);
          announce(result.queued ? 'The export has been queued.'
            : 'This build mounts no export endpoint, so nothing was queued.');
        },
      }));
    }

    async function loadTotal() {
      renderChips([]);
      await totalLoader.run(() => spec.fetchTotal(filters), {
        render: (data, result) => {
          renderChips(result.unapplied);
          const summary = (data && (data.summary || data.totals)) || null;
          const { rows } = readRows(data, ['lines']);
          renderCards(summary, rows);
          if (!summary) {
            /* The source answered and reported no summary. That is not an
               empty result — there may be plenty committed — so it is
               deliberately NOT rendered as empty: the cards say "not reported"
               and the loader stays ready. */
            return true;
          }
          return true;
        },
        onState: (state) => {
          if (state !== 'ready' && state !== 'stale') {
            while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
            while (quality.firstChild) quality.removeChild(quality.firstChild);
          }
        },
      });
    }

    async function loadBuckets() {
      buckets.el.hidden = false;
      buckets.renderSkeleton();
      const state = await bucketLoader.run(() => getAgeing(spec.kind, filters), {
        render: (data) => {
          const { rows, readable } = readRows(data, ['buckets']);
          if (!readable) {
            throw new Error('The ageing report carried no recognisable bucket array.');
          }
          if (!rows.length) { buckets.renderRows([]); buckets.el.hidden = true; return false; }
          buckets.renderRows(rows);
          return true;
        },
      });
      if (state !== 'ready' && state !== 'stale') {
        buckets.renderRows([]);
        buckets.el.hidden = true;
      }
      if (state === 'unavailable') {
        /* Said once more, at the place the distribution would have been,
           because the unavailable block above explains WHICH route is missing
           and this explains why nothing was assembled from what IS available. */
        bucketLoader.notes.appendChild(h('p', { class: 'xs muted' },
          'The measured total above is real and is shown. Its distribution across ageing buckets '
          + 'is not: bucketing the documents this build can return would bucket one page of them '
          + 'while the column headings claimed to describe the whole portfolio. A wrong '
          + 'distribution under a right total is harder to catch than a missing one, so nothing '
          + 'is shown here.'));
      }
    }

    wireExport();
    loadTotal();
    loadBuckets();
  };
}

export { METRIC_DEFINITION, METRIC_LABEL };
