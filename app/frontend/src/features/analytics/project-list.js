/* app/frontend/src/features/analytics/project-list.js
   SCR-04 — CAPEX Project List.

   Named verbatim in research/30_contracts/C8_screens.json as "CAPEX Project
   List", so this screen carries its real number.

   THE LIST IS ALSO THE DRILL-DOWN TARGET
   --------------------------------------
   SCR-01's "Projects in scope" card links here, and SCR-01's money cards link
   through their own metric to the screen that holds those records. That makes
   this list a place a reader ARRIVES at with a FilterSet already in the URL,
   not only a place they start from — so the filter bar is initialised from the
   URL, the chips state what is narrowing the rows, and the count is stated
   against the filters rather than presented as "all projects".

   PAGINATION IS THE SERVER'S, AND SO IS THE TOTAL
   -----------------------------------------------
   The Wave 7 contract requires cursor pagination, deterministic and tie-broken
   on a unique column. When the reporting service answers, `next_cursor` drives
   the More control and the totals footer stays the SERVER's totals — the total
   over the whole filtered population, not the sum of the page.

   That distinction is stated on the footer itself, because it is exactly where
   a reader would otherwise assume the two agree. `assertSumsBack()` runs
   anyway and reports the difference; on a paginated result a difference is
   EXPECTED and is reported as an explanation rather than as a fault, which is
   why the message says which way the difference runs.

   The ledger fallback returns every project in one response with no cursor, so
   on that source the two do agree, and the check proves it rather than
   assuming it.
*/

import { h, text } from '../../core/dom.js';
import { createLoader, requireRoot } from './analytics-screen.js';
import {
  card, countCard, createAnnouncer, exportButton, metricCard, reconciliationBlock,
} from './analytics-kit.js';
import {
  createFilterBar, drilldownHref, filterChips, GROUP_DIMENSIONS, readDrilldownContext,
  readFilters, writeFilters,
} from './analytics-filters.js';
import { assertSumsBack } from './analytics-metrics.js';
import {
  METRIC_DEFINITION, METRIC_LABEL, projectRow, readRows, readTotals,
} from './analytics-shapes.js';
import { createProjectTable, METRIC_TARGET, totalsFooter } from './portfolio-table.js';
import {
  exportAvailable, getPortfolio, queueExport, REPORT_IDS,
} from './analytics-api.js';

const COLUMNS = ['budget', 'ordered', 'commitment', 'actual', 'received_not_billed', 'available'];

const CARDS = [
  { key: 'budget', accent: 'info' },
  { key: 'commitment', accent: 'info' },
  { key: 'actual', accent: 'info' },
  { key: 'available', accent: 'safe' },
];

const FILTER_FIELDS = [
  'entity_ids', 'plant_ids', 'location_ids', 'project_ids', 'budget_head_ids',
  'category_ids', 'lifecycle_statuses', 'approval_statuses', 'date_from', 'date_to',
  'period_ids', 'group_by', 'limit', 'sort',
];

export function mountProjectList(root) {
  requireRoot(root, 'SCR-04 CAPEX Project List');
  const announce = createAnnouncer('analyticsLiveRegion');

  let filters = readFilters();
  const context = readDrilldownContext();
  const getFilters = () => filters;

  const arrival = h('div', { class: 'analytics-arrival' });
  const chips = h('div', { class: 'analytics-chip-host' });
  const tiles = h('div', { id: 'listTiles' });
  const quality = h('div', { class: 'analytics-quality-host' });
  const pager = h('div', { class: 'btn-row analytics-pager' });
  const exportHost = h('span', { class: 'analytics-export-host' });

  const table = createProjectTable({
    metrics: COLUMNS,
    getFilters,
    caption: 'CAPEX projects matching the current filters, with budget, ordered value, open '
      + 'commitment, actual CWIP, unbilled receipts and available budget',
  });

  const filterBar = createFilterBar({
    idPrefix: 'plist',
    fields: FILTER_FIELDS,
    value: filters,
    groupOptions: GROUP_DIMENSIONS,
    onApply: (next) => apply(next),
  });

  const loader = createLoader({
    id: 'listStatus',
    glyph: '▤',
    what: 'The CAPEX project list',
    emptyMessage: 'No CAPEX project matches these filters. Clear them to list every project your '
      + 'access scope reaches.',
    loadingMessage: 'Loading projects…',
    onRetry: () => load(),
    announce,
  });

  /* A reader who ARRIVED here from a card is told which card, so the rows in
     front of them are anchored to the figure they clicked. Without it a
     drill-down is just a list that happens to be filtered. */
  if (context.metric) {
    arrival.appendChild(h('div', { class: 'msg msg-info', role: 'status' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, `Drill-down from ${METRIC_LABEL[context.metric] || context.metric}.`),
        text(' These rows are the source records behind that figure, under the same filters. '),
        METRIC_DEFINITION[context.metric] ? text(METRIC_DEFINITION[context.metric]) : null,
      ].filter(Boolean)),
    ]));
  }

  root.appendChild(card('listTitle', 'CAPEX Project List', [
    arrival,
    filterBar.el,
    chips,
    loader.el,
    tiles,
    quality,
    table.el,
    pager,
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
    chips.appendChild(filterChips(filters, unapplied));
  }

  function renderCards(totals, rows, paged) {
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
        href: drilldownHref(METRIC_TARGET[spec.key] || 'analytics-project-object', filters, {
          metric: spec.key, dimension: 'project',
        }),
      }));
      const check = assertSumsBack(value, rows, spec.key);
      check.label = `${METRIC_LABEL[spec.key]} · card versus the ${rows.length} row(s) on this page`;
      checks.push(check);
    }
    band.appendChild(countCard({
      label: 'Projects listed',
      count: rows.length,
      sub: paged
        ? 'on this page — the totals above are the whole filtered population, not this page'
        : 'the whole of this filtered result; the totals above are the sum of exactly these rows',
      metric: 'project_count',
    }));
    tiles.appendChild(band);

    const failed = checks.filter((c) => !c.ok);
    if (!failed.length) {
      quality.appendChild(reconciliationBlock({
        ok: true,
        checked: true,
        message: `All ${checks.length} totals above are exactly the sum of the ${rows.length} `
          + 'row(s) listed.',
      }));
      return;
    }
    if (paged) {
      /* A DIFFERENCE ON A PAGED RESULT IS EXPECTED, AND IS STILL SHOWN.
         The totals cover the population and the rows cover the page, so they
         are not meant to match. Suppressing the check here would be
         convenient and would also remove the only signal that would catch a
         genuine mismatch on the LAST page, where there is no more to fetch and
         the two should agree. So it is reported, and labelled as expected. */
      quality.appendChild(h('div', { class: 'msg msg-info', role: 'status' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
        h('div', { class: 'body' }, [
          h('strong', {}, 'The totals above cover more rows than this page shows.'),
          text(` ${failed.length} of ${checks.length} totals exceed the sum of the rows on screen, `
            + 'which is what a paginated result is expected to look like: the figures describe the '
            + 'whole filtered population and the rows describe one page of it. Fetch the remaining '
            + 'pages to see every row behind them.'),
        ]),
      ]));
      return;
    }
    for (const check of failed) quality.appendChild(reconciliationBlock(check));
  }

  function renderPager(nextCursor) {
    while (pager.firstChild) pager.removeChild(pager.firstChild);
    if (!nextCursor) {
      pager.appendChild(h('span', { class: 'xs muted' },
        'This is the whole of the filtered result — there is no further page.'));
      return;
    }
    pager.appendChild(h('button', {
      type: 'button',
      class: 'btn-sm',
      onClick: () => apply({ ...filters, cursor: nextCursor }),
    }, 'Next page'));
    pager.appendChild(h('span', { class: 'xs muted' },
      'Cursor-based, ordered deterministically by the server. A page boundary never moves a row '
      + 'between pages the way an offset does.'));
  }

  async function wireExport() {
    const availability = await exportAvailable();
    while (exportHost.firstChild) exportHost.removeChild(exportHost.firstChild);
    exportHost.appendChild(exportButton({
      availability,
      reportId: REPORT_IDS.projectList,
      onExport: async () => {
        /* The export carries the FilterSet WITHOUT the cursor: an export is of
           the filtered population, not of the page the reader happens to be
           looking at. Carrying the cursor would silently export one page and
           label the file with the filters, which is a file that looks complete
           and is not. */
        const result = await queueExport(REPORT_IDS.projectList, { ...filters, cursor: null });
        announce(result.queued
          ? 'The export has been queued for the whole filtered result, not just this page.'
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

    await loader.run(() => getPortfolio(filters, REPORT_IDS.projectList), {
      render: (data, result) => {
        renderChips(result.unapplied);
        const { rows: raw, readable } = readRows(data, ['projects']);
        if (!readable) {
          throw new Error('The reporting response carried no recognisable row array, so this '
            + 'screen cannot tell an empty list from a response it could not read.');
        }
        const rows = raw.map(projectRow);
        const totals = readTotals(data);
        const nextCursor = (data && (data.next_cursor || data.nextCursor)) || null;
        renderCards(totals, rows, !!nextCursor);
        renderPager(nextCursor);
        if (!rows.length) { table.renderRows([]); table.el.hidden = true; return false; }
        table.renderRows(rows);
        totalsFooter(table, totals, COLUMNS);
        announce(`${rows.length} project${rows.length === 1 ? '' : 's'} listed`
          + `${nextCursor ? ', with a further page available' : ''}.`);
        return true;
      },
      onState: (state) => {
        if (state !== 'ready' && state !== 'stale') {
          table.renderRows([]);
          table.el.hidden = true;
          while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
          while (pager.firstChild) pager.removeChild(pager.firstChild);
        }
      },
    });

    loading = false;
  }

  wireExport();
  load();
}
