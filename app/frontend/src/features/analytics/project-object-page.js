/* app/frontend/src/features/analytics/project-object-page.js
   SCR-05 — CAPEX Project Object Page.

   Named verbatim in research/30_contracts/C8_screens.json as "CAPEX Project
   Object Page", so this screen carries its real number.

   THE OBJECT PAGE IS WHERE A DRILL-DOWN LANDS
   -------------------------------------------
   Every project link on SCR-01, SCR-02 and SCR-04 arrives here, carrying the
   FilterSet that produced the figure that was clicked. So this screen is
   written for arrival: it reads the project from the URL, states which metric
   the reader came from when they came from one, and keeps the filters intact
   so the next hop — into the WBS tree, the CWIP ledger, the ageing — carries
   the same question forward.

   OWN VALUE AND ROLLUP ARE SHOWN SEPARATELY, ALWAYS
   -------------------------------------------------
   The project total is the rollup of the whole hierarchy. The elements listed
   below each carry their own rollup. Adding the listed elements to the project
   total, or adding a parent element to its children, double counts — so the
   two figures are never placed in a way that invites addition, the root
   elements are identified as roots, and the sums-back check compares the
   project total against THE ROOTS rather than against every row.

   WHAT IS NOT ON THIS SCREEN
   --------------------------
   No approval action, no budget revision, no capitalisation control. This is a
   reporting surface. The mutating screens for those are SCR-11, SCR-12, SCR-20
   and SCR-21, and they belong to another agent in this wave; putting a button
   here that posted to a route this agent does not own would put a mutating
   route on screen without its entry in `tests/test_api_auth.py::MUTATING_ROUTES`,
   which is the matrix that proves every mutation is gated.
*/

import { h, text } from '../../core/dom.js';
import { createLoader, requireRoot } from './analytics-screen.js';
import {
  card, countCard, createAnnouncer, exportButton, keyValues, metricCard, reconciliationBlock,
} from './analytics-kit.js';
import {
  createFilterBar, drilldownHref, filterChips, readDrilldownContext, readFilters, screenHref,
  writeFilters,
} from './analytics-filters.js';
import { assertSumsBack } from './analytics-metrics.js';
import {
  flattenWbs, METRIC_DEFINITION, METRIC_LABEL, readTotals,
} from './analytics-shapes.js';
import { createWbsTree } from './wbs-tree.js';
import { METRIC_TARGET } from './portfolio-table.js';
import {
  exportAvailable, getWbs, queueExport, SCREENS,
} from './analytics-api.js';

const CARDS = ['budget', 'commitment', 'actual', 'received_not_billed', 'pr_reserved', 'available'];

export function mountProjectObjectPage(root) {
  requireRoot(root, 'SCR-05 CAPEX Project Object Page');
  const announce = createAnnouncer('analyticsLiveRegion');

  let filters = readFilters();
  const context = readDrilldownContext();
  const getFilters = () => filters;
  const projectOf = () => (filters.project_ids && filters.project_ids.length
    ? filters.project_ids[0] : null);

  const arrival = h('div', { class: 'analytics-arrival' });
  const chips = h('div', { class: 'analytics-chip-host' });
  const identity = h('div', { class: 'analytics-identity' });
  const tiles = h('div', { id: 'objTiles' });
  const quality = h('div', { class: 'analytics-quality-host' });
  const prompt = h('div', { class: 'analytics-prompt' });
  const exportHost = h('span', { class: 'analytics-export-host' });

  const tree = createWbsTree({
    metrics: ['budget', 'commitment', 'actual', 'available'],
    getFilters,
    caption: 'The top of this project’s WBS hierarchy, with the budget and exposure rolled '
      + 'up to each element',
    rowHref: (row) => drilldownHref('analytics-wbs-element', getFilters(), {
      dimension: 'wbs', narrowKey: 'wbs_paths', narrowValue: row.wbs_code,
    }),
  });

  const filterBar = createFilterBar({
    idPrefix: 'obj',
    fields: ['project_ids', 'budget_head_ids', 'category_ids', 'date_from', 'date_to', 'period_ids'],
    value: filters,
    onApply: (next) => apply(next),
  });

  const loader = createLoader({
    id: 'objStatus',
    glyph: '▤',
    what: 'This CAPEX project',
    emptyMessage: 'This project has no WBS element matching these filters.',
    loadingMessage: 'Loading the project…',
    onRetry: () => load(),
    announce,
  });

  if (context.metric) {
    arrival.appendChild(h('div', { class: 'msg msg-info', role: 'status' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, `Arrived from ${METRIC_LABEL[context.metric] || context.metric}.`),
        text(' The filters that produced that figure are intact below, so what follows is the '
          + 'same question narrowed to this project. '),
        METRIC_DEFINITION[context.metric] ? text(METRIC_DEFINITION[context.metric]) : null,
      ].filter(Boolean)),
    ]));
  }

  root.appendChild(card('objTitle', 'CAPEX Project Object Page', [
    arrival,
    filterBar.el,
    chips,
    prompt,
    loader.el,
    identity,
    tiles,
    quality,
    h('h3', { class: 'analytics-subhead' }, 'WBS hierarchy'),
    h('p', { class: 'xs muted' },
      'Each element shows the rollup of everything beneath it. The project figures above are the '
      + 'sum of the ROOT elements only — adding the listed elements together would count every '
      + 'branch once for each level it appears at.'),
    tree.el,
    crossLinks(),
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

  function crossLinks() {
    return h('nav', { class: 'analytics-crosslinks', 'aria-label': 'Related views for this project' }, [
      h('span', { class: 'muted' }, 'This project in:'),
      h('a', {
        class: 'linkish',
        href: screenHref('analytics-wbs-tree', filters),
      }, 'the WBS tree table'),
      h('a', { class: 'linkish', href: screenHref('analytics-cwip-ledger', filters) },
        'the CWIP ledger'),
      h('a', { class: 'linkish', href: screenHref('analytics-commitment-ageing', filters) },
        'open commitment ageing'),
      h('a', { class: 'linkish', href: screenHref('analytics-exceptions', filters) },
        'the exception monitor'),
      h('a', { class: 'linkish', href: '#integration-reconciliation' },
        'commitment-to-actual reconciliation'),
    ]);
  }

  function renderIdentity(data, projectId) {
    while (identity.firstChild) identity.removeChild(identity.firstChild);
    /* Identity fields come from whatever the source reported. A field the
       source did not send is OMITTED rather than dashed: keyValues() drops a
       null pair, so the list shows what is known and does not imply that the
       rest is empty. */
    identity.appendChild(keyValues([
      ['Project id', h('span', { class: 'mono' }, String(projectId))],
      ['CAPEX code', data && data.capex_code ? h('span', { class: 'mono' }, String(data.capex_code)) : null],
      ['Name', data && data.name ? text(String(data.name)) : null],
      ['Entity', data && (data.entity || data.entity_name) ? text(String(data.entity || data.entity_name)) : null],
      ['Plant', data && (data.plant || data.plant_name) ? text(String(data.plant || data.plant_name)) : null],
      ['Status', data && data.status ? text(String(data.status)) : null],
    ]));
  }

  function renderCards(totals, rows) {
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
    while (quality.firstChild) quality.removeChild(quality.firstChild);
    const roots = rows.filter((r) => r.depth === 0);
    const band = h('div', { class: 'tiles analytics-tiles' });
    const checks = [];
    for (const key of CARDS) {
      const value = totals ? totals[key] : null;
      band.appendChild(metricCard({
        label: METRIC_LABEL[key],
        paise: value === undefined ? null : value,
        sub: METRIC_DEFINITION[key],
        accent: key === 'available' ? 'safe'
          : (key === 'received_not_billed' || key === 'pr_reserved') ? 'watch' : 'info',
        metric: key,
        href: drilldownHref(METRIC_TARGET[key] || 'analytics-wbs-tree', filters, {
          metric: key, dimension: 'wbs',
        }),
      }));
      const check = assertSumsBack(value, roots, key);
      check.label = `${METRIC_LABEL[key]} · project total versus its ${roots.length} root element(s)`;
      checks.push(check);
    }
    band.appendChild(countCard({
      label: 'WBS elements',
      count: rows.length,
      sub: `at every level, of which ${roots.length} are roots`,
      metric: 'wbs_count',
      // See executive-dashboard.js: every tile declares which figure was
      // clicked.
      href: drilldownHref('analytics-wbs-tree', filters, { metric: 'wbs_count' }),
    }));
    tiles.appendChild(band);

    const failed = checks.filter((c) => !c.ok);
    if (failed.length) {
      for (const check of failed) quality.appendChild(reconciliationBlock(check));
    } else if (checks.length) {
      quality.appendChild(reconciliationBlock({
        ok: true,
        checked: true,
        message: `All ${checks.length} project figures are exactly the sum of the ${roots.length} `
          + 'root element(s) beneath them.',
      }));
    }
  }

  function renderPrompt() {
    while (prompt.firstChild) prompt.removeChild(prompt.firstChild);
    prompt.appendChild(h('div', { class: 'msg msg-info', role: 'status', 'data-state': 'unstarted' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, 'Choose a CAPEX project.'),
        h('div', {}, [
          text('This page shows one project. Nothing has been asked for yet, so nothing is '
            + 'shown — this is not a project with no data. '),
          h('a', { class: 'linkish', href: screenHref('analytics-project-list', filters) },
            'Pick one from the project list'),
          text('.'),
        ]),
      ]),
    ]));
  }

  async function wireExport() {
    const availability = await exportAvailable();
    while (exportHost.firstChild) exportHost.removeChild(exportHost.firstChild);
    exportHost.appendChild(exportButton({
      availability,
      report: SCREENS.projectDetail,
      onExport: async () => {
        const result = await queueExport(SCREENS.projectDetail, filters);
        announce(result.queued ? 'The export has been queued.'
          : 'This build mounts no export endpoint, so nothing was queued.');
      },
    }));
  }

  let loading = false;

  async function load() {
    if (loading) return;
    renderChips([]);
    const projectId = projectOf();
    if (!projectId) {
      renderPrompt();
      loader.host.set('ready');
      loader.el.setAttribute('data-analytics-state', 'unstarted');
      tree.setRows([]);
      tree.el.hidden = true;
      while (identity.firstChild) identity.removeChild(identity.firstChild);
      while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
      announce('Choose a CAPEX project to open its object page.');
      return;
    }
    while (prompt.firstChild) prompt.removeChild(prompt.firstChild);
    loading = true;
    tree.el.hidden = false;
    tree.renderSkeleton();

    await loader.run(() => getWbs(projectId, filters, SCREENS.projectDetail), {
      render: (data, result) => {
        renderChips(result.unapplied);
        const nested = Array.isArray(data && data.tree) ? data.tree
          : Array.isArray(data && data.rows) ? data.rows
            : Array.isArray(data) ? data : null;
        if (nested === null) {
          throw new Error('The project response carried no recognisable WBS tree, so this screen '
            + 'cannot tell a project with no structure from a response it could not read.');
        }
        const { rows, truncated } = flattenWbs(nested);
        const totals = readTotals(data);
        renderIdentity(data, projectId);
        renderCards(totals, rows);
        if (truncated) {
          quality.appendChild(h('div', { class: 'msg msg-warning', role: 'alert' }, [
            h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
            h('div', { class: 'body' }, 'This hierarchy is deeper than the supported limit and '
              + 'was truncated for display. The figures above are complete; the rows below are '
              + 'not.'),
          ]));
        }
        if (!rows.length) { tree.setRows([]); tree.el.hidden = true; return false; }
        // See executive-dashboard.js: `onState('loading')` hides this, and
        // only the success path can put it back.
        tree.el.hidden = false;
        // The object page opens at the top two levels: it is a summary of the
        // project, and the whole tree is one click away in SCR-07.
        tree.setRows(rows, { collapseBelow: 1 });
        announce(`${rows.length} WBS element(s) in ${projectId}.`);
        return true;
      },
      onState: (state) => {
        if (state !== 'ready' && state !== 'stale') {
          tree.setRows([]);
          tree.el.hidden = true;
          while (identity.firstChild) identity.removeChild(identity.firstChild);
          while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
        }
      },
    });

    loading = false;
  }

  wireExport();
  load();
}
