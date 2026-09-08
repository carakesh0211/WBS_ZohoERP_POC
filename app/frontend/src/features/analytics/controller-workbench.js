/* app/frontend/src/features/analytics/controller-workbench.js
   SCR-02 — Project Controller Workbench.

   Named verbatim in research/30_contracts/C8_screens.json as "Project
   Controller Workbench", so this screen carries its real number.

   WHO THIS SCREEN IS FOR, AND WHAT THAT CHANGES
   ---------------------------------------------
   SCR-01 answers "how is the portfolio". This answers "what do I have to do
   about it". A controller is not reading for reassurance: they are looking for
   the projects that are about to breach, the receipts that have not been
   billed, and the reservations that will land next.

   Two consequences follow, and both are visible in the code.

   1. ALL TEN METRICS, NOT SIX. The executive view shows the four buckets that
      must never be added together. This one also shows `original`, `revisions`,
      `ordered`, `received` and `pr_reserved`, because a controller's question
      is often exactly "how much of this budget is revision rather than
      original", and a screen that hides the decomposition forces them into a
      spreadsheet, which is where reconciliations go to die.

   2. IT SORTS BY EXPOSURE, NOT BY CODE. The default order is the server's, and
      the sort control asks the SERVER to reorder — it does not reorder the
      page in the browser. A client-side sort over a paginated result orders
      the page and looks exactly like ordering the population; the row a
      controller most needs is then the row that was never fetched.

   PERMISSION
   ----------
   Gated on `budget.check` rather than `budget.read`. That is not decoration:
   `budget.check` is the permission the availability control itself takes, and
   this workbench exists to decide whether a commitment can be made. Every role
   holds `budget.read`, so gating on it would be gating on nothing, and the
   difference between the two gates is what `tests/vrt/analytics.spec.js`
   exercises with two identities rather than one.

   THE GATE HERE IS PRESENTATIONAL. The server decides. A route this shell
   refuses to render is a courtesy that keeps a bookmarked URL from painting a
   screen whose data the server would refuse anyway; it is not what keeps the
   data safe.
*/

import { h, text } from '../../core/dom.js';
import { createLoader, requireRoot } from './analytics-screen.js';
import {
  card, countCard, createAnnouncer, exportButton, metricCard, reconciliationBlock,
} from './analytics-kit.js';
import {
  createFilterBar, drilldownHref, filterChips, GROUP_DIMENSIONS, readFilters, screenHref,
  writeFilters,
} from './analytics-filters.js';
import { assertSumsBack, inr } from './analytics-metrics.js';
import {
  METRIC_DEFINITION, METRIC_LABEL, projectRow, readAlerts, readRows, readTotals,
} from './analytics-shapes.js';
import { createProjectTable, METRIC_TARGET, totalsFooter } from './portfolio-table.js';
import {
  exportAvailable, getPortfolio, queueExport, SCREENS,
} from './analytics-api.js';

const SCREEN_ID = 'analytics-controller';

/** All ten, in the order the Wave 7 contract lists them. */
const COLUMNS = [
  'budget', 'original', 'revisions', 'ordered', 'commitment',
  'actual', 'received', 'received_not_billed', 'pr_reserved', 'available',
];

/** The four the controller acts on first. */
const CARDS = [
  { key: 'available', accent: 'safe' },
  { key: 'commitment', accent: 'info' },
  { key: 'received_not_billed', accent: 'watch' },
  { key: 'pr_reserved', accent: 'watch' },
];

const FILTER_FIELDS = [
  'entity_ids', 'plant_ids', 'project_ids', 'wbs_paths', 'budget_head_ids',
  'category_ids', 'vendor_ids', 'lifecycle_statuses', 'date_from', 'date_to',
  'period_ids', 'group_by',
];

export function mountControllerWorkbench(root) {
  requireRoot(root, 'SCR-02 Project Controller Workbench');
  const announce = createAnnouncer('analyticsLiveRegion');

  let filters = readFilters();
  const getFilters = () => filters;

  const chips = h('div', { class: 'analytics-chip-host' });
  const tiles = h('div', { id: 'ctrlTiles' });
  const quality = h('div', { class: 'analytics-quality-host' });
  const worklist = h('div', { class: 'analytics-worklist' });
  const exportHost = h('span', { class: 'analytics-export-host' });

  const table = createProjectTable({
    metrics: COLUMNS,
    getFilters,
    caption: 'Every project in scope with the full decomposition of its budget and exposure: '
      + 'original, revisions, ordered, open commitment, actual CWIP, received, unbilled receipts, '
      + 'PR reservations and available budget',
  });

  const filterBar = createFilterBar({
    idPrefix: 'ctrl',
    fields: FILTER_FIELDS,
    value: filters,
    groupOptions: GROUP_DIMENSIONS,
    onApply: (next) => apply(next),
  });

  const loader = createLoader({
    id: 'ctrlStatus',
    glyph: '◎',
    what: 'The controller workbench',
    emptyMessage: 'No project matches these filters. Nothing needs your attention in this slice — '
      + 'widen the filters to check a wider one.',
    loadingMessage: 'Loading the projects you control…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(card('ctrlTitle', 'Project Controller Workbench', [
    h('p', { class: 'muted small' },
      'The full decomposition, because a controller\'s question is usually which part of a budget '
      + 'is revision rather than original, and which exposure is committed rather than spent. '
      + 'Sorting asks the server to reorder — a sort applied in the browser would order this page '
      + 'and look exactly like ordering the portfolio.'),
    filterBar.el,
    chips,
    loader.el,
    tiles,
    quality,
    worklist,
    table.el,
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
      const check = assertSumsBack(value, rows, spec.key);
      check.label = `${METRIC_LABEL[spec.key]} · card versus the ${rows.length} row(s) below`;
      checks.push(check);
    }

    /* The count of projects whose exposure exceeds their budget. Counted from
       the rows the server returned, and LABELLED as such: it is a count of
       what is on this page, not a portfolio statistic, and saying so is the
       difference between a useful number and a misleading one. */
    const breached = rows.filter((r) => r.utilisation_bp !== null && r.utilisation_bp > 10000);
    band.appendChild(countCard({
      label: 'Over budget, in these rows',
      count: breached.length,
      sub: `of the ${rows.length} row(s) returned — a count of what is on screen, not a `
        + 'portfolio-wide figure',
      accent: breached.length ? 'breach' : 'safe',
      metric: 'breached_count',
      href: screenHref('analytics-exceptions', filters),
    }));

    tiles.appendChild(band);

    const failed = checks.filter((c) => !c.ok);
    if (failed.length) {
      for (const check of failed) quality.appendChild(reconciliationBlock(check));
    } else if (checks.length) {
      quality.appendChild(reconciliationBlock({
        ok: true,
        checked: true,
        message: `All ${checks.length} figures above are exactly the sum of the ${rows.length} `
          + 'row(s) below them.',
      }));
    }
  }

  /**
   * The worklist: what a controller has to act on, itemised.
   *
   * Each list stays SEPARATE and none is folded into a single count. A budget
   * exception, an over-billed line and an unbilled receipt have three
   * different owners and three different remedies, and one number covering all
   * three is a number nobody can act on.
   */
  function renderWorklist(alerts) {
    while (worklist.firstChild) worklist.removeChild(worklist.firstChild);
    if (!alerts.present) {
      worklist.appendChild(h('p', { class: 'xs muted' },
        'The source that answered this screen does not report a worklist, so none is shown. '
        + 'That is not the same as an empty worklist.'));
      return;
    }
    const groups = [
      ['Budget exceptions', alerts.budget_exceptions, 'error',
        (r) => `${r.pr_number || r.pr_id || 'A purchase request'} — ${r.exception_reason || 'exceeds available budget'}`],
      ['Bills exceeding their order', alerts.over_billed_lines, 'error',
        (r) => `${r.po_number || '—'} (${r.wbs_code || '—'}): billed ${inr(r.billed_paise)} against ${inr(r.ordered_paise)} ordered`],
      ['Received, not billed', alerts.received_not_billed_lines, 'warning',
        (r) => `${r.wbs_code || '—'} on ${r.po_number || '—'}: ${inr(r.received_not_billed_paise)} awaiting a vendor bill`],
      ['Revisions awaiting approval', alerts.pending_revisions, 'info',
        (r) => `${r.revision_id || '—'} (${r.kind || 'revision'}) — ${r.reason || 'no reason recorded'}`],
    ];
    const list = h('dl', { class: 'kv analytics-kv' });
    let any = false;
    for (const [label, rows, , describe] of groups) {
      list.appendChild(h('dt', {}, label));
      if (!rows.length) {
        list.appendChild(h('dd', { class: 'muted' }, 'none in this scope'));
        continue;
      }
      any = true;
      list.appendChild(h('dd', {}, h('ul', { class: 'analytics-worklist-items' },
        rows.slice(0, 8).map((r) => h('li', {}, text(describe(r)))))));
      if (rows.length > 8) {
        list.appendChild(h('dd', { class: 'xs muted' },
          `and ${rows.length - 8} more — open the exception monitor for the full list`));
      }
    }
    worklist.appendChild(h('section', { 'aria-labelledby': 'ctrlWorklistTitle' }, [
      h('h3', { id: 'ctrlWorklistTitle', class: 'analytics-subhead' }, 'Requiring your attention'),
      list,
      h('p', { class: 'xs muted' }, any
        ? 'Each list is kept separate: these are different problems with different owners, and a '
          + 'single combined count would be a number nobody could act on.'
        : 'Every list above was reported and every one is empty. This is a measured "none".'),
    ]));
  }

  async function wireExport() {
    const availability = await exportAvailable();
    while (exportHost.firstChild) exportHost.removeChild(exportHost.firstChild);
    exportHost.appendChild(exportButton({
      availability,
      report: SCREENS.controller,
      onExport: async () => {
        const result = await queueExport(SCREENS.controller, filters);
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

    await loader.run(() => getPortfolio(filters, SCREENS.controller), {
      render: (data, result) => {
        renderChips(result.unapplied);
        const { rows: raw, readable } = readRows(data, ['projects']);
        if (!readable) {
          throw new Error('The reporting response carried no recognisable row array, so this '
            + 'screen cannot tell an empty worklist from a response it could not read.');
        }
        const rows = raw.map(projectRow);
        const totals = readTotals(data);
        renderCards(totals, rows);
        renderWorklist(readAlerts(data));
        if (!rows.length) { table.renderRows([]); table.el.hidden = true; return false; }
        // See executive-dashboard.js: `onState('loading')` hides this, and
        // only the success path can put it back.
        table.el.hidden = false;
        table.renderRows(rows);
        totalsFooter(table, totals, COLUMNS);
        announce(`${rows.length} project${rows.length === 1 ? '' : 's'} under your control in this filter.`);
        return true;
      },
      onState: (state) => {
        if (state !== 'ready' && state !== 'stale') {
          table.renderRows([]);
          table.el.hidden = true;
          while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
          while (worklist.firstChild) worklist.removeChild(worklist.firstChild);
        }
      },
    });

    loading = false;
  }

  wireExport();
  load();
}
