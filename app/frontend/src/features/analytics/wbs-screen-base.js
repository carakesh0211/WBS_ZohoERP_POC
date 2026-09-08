/* app/frontend/src/features/analytics/wbs-screen-base.js
   The shell SCR-06 and SCR-07 share.

   TWO C8 SCREENS, ONE IMPLEMENTATION, SAID OUT LOUD
   -------------------------------------------------
   C8_screens.json lists "WBS Hierarchy Explorer" (SCR-06) and "WBS Tree Table"
   (SCR-07) as two screens, and they are two screens: two ids, two routes, two
   titles, two entries in the inventory. What they are not is two different
   pieces of behaviour. Both read one project's WBS, both render a tree table,
   both drill down from every money cell. They differ in DEFAULTS:

     SCR-06 opens SHALLOW — roots plus one level, everything else collapsed —
            and carries the five columns a reader navigating a structure needs.
            It is for finding your way to an element.
     SCR-07 opens FLAT — every node expanded — and carries all ten metrics. It
            is for reading the numbers across a whole hierarchy at once.

   Writing that twice would be two chances for the double-counting rule, the
   CSSOM indentation or the drill-down target to drift between them, and the
   drift would be invisible: both would still render a tree. So the difference
   is a configuration object and the sameness is a fact rather than a
   coincidence. If a later brief makes them genuinely different screens, this
   file is where the fork happens and it will be a deliberate one.

   A PROJECT IS REQUIRED, AND ITS ABSENCE IS A STATE
   -------------------------------------------------
   Both screens read `/api/projects/{project_id}/wbs`, so without a project
   there is nothing to ask for. That is not an empty result and not an error —
   it is an unstarted query, and it renders as an explicit prompt rather than
   as a spinner that never resolves or an empty table that implies the project
   has no WBS. The integration feature hit exactly this: a loader left in
   `loading` for a query nobody started timed out every wait on the screen.
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
  flattenWbs, METRIC_DEFINITION, METRIC_LABEL, readTotals,
} from './analytics-shapes.js';
import { createWbsTree } from './wbs-tree.js';
import { METRIC_TARGET } from './portfolio-table.js';
import { exportAvailable, getWbs, queueExport } from './analytics-api.js';

/**
 * @param {Object} spec
 * @param {string} spec.screen - "SCR-06 WBS Hierarchy Explorer".
 * @param {string} spec.idPrefix
 * @param {string} spec.title
 * @param {string} spec.intro
 * @param {string[]} spec.metrics - money columns.
 * @param {string[]} spec.cards - metric keys rendered as cards.
 * @param {number|null} spec.collapseBelow - null opens flat.
 * @param {string} spec.report
 */
export function createWbsScreen(spec) {
  return function mount(root) {
    requireRoot(root, spec.screen);
    const announce = createAnnouncer('analyticsLiveRegion');

    let filters = readFilters();
    const getFilters = () => filters;
    const projectOf = () => (filters.project_ids && filters.project_ids.length
      ? filters.project_ids[0] : null);

    const chips = h('div', { class: 'analytics-chip-host' });
    const tiles = h('div', { id: `${spec.idPrefix}Tiles` });
    const quality = h('div', { class: 'analytics-quality-host' });
    const prompt = h('div', { class: 'analytics-prompt' });
    const controls = h('div', { class: 'btn-row analytics-tree-controls' });
    const exportHost = h('span', { class: 'analytics-export-host' });

    const tree = createWbsTree({
      metrics: spec.metrics,
      getFilters,
      caption: `The WBS hierarchy of the selected project, one row per element, with the budget `
        + `and exposure rolled up to each node`,
      rowHref: (row) => drilldownHref('analytics-wbs-element', getFilters(), {
        dimension: 'wbs', narrowKey: 'wbs_paths', narrowValue: row.wbs_code,
      }),
    });

    const filterBar = createFilterBar({
      idPrefix: spec.idPrefix,
      fields: ['project_ids', 'wbs_paths', 'budget_head_ids', 'category_ids',
        'lifecycle_statuses', 'date_from', 'date_to', 'period_ids'],
      value: filters,
      onApply: (next) => apply(next),
    });

    const loader = createLoader({
      id: `${spec.idPrefix}Status`,
      glyph: '⌗',
      what: 'The WBS hierarchy',
      emptyMessage: 'This project has no WBS element matching these filters.',
      loadingMessage: 'Loading the WBS hierarchy…',
      onRetry: () => load(),
      announce,
    });

    controls.appendChild(h('button', {
      type: 'button', class: 'btn-sm', onClick: () => { tree.expandAll(); announceCount(); },
    }, 'Expand all'));
    controls.appendChild(h('button', {
      type: 'button', class: 'btn-sm', onClick: () => { tree.collapseAll(1); announceCount(); },
    }, 'Collapse to level 1'));

    root.appendChild(card(`${spec.idPrefix}Title`, spec.title, [
      h('p', { class: 'muted small' }, spec.intro),
      filterBar.el,
      chips,
      prompt,
      loader.el,
      tiles,
      quality,
      controls,
      tree.el,
    ], { actions: exportHost }));

    function announceCount() {
      announce(`${tree.visibleCount()} WBS element(s) visible.`);
    }

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
      const roots = rows.filter((r) => r.depth === 0);
      const checks = [];
      for (const key of spec.cards) {
        const value = totals ? totals[key] : null;
        band.appendChild(metricCard({
          label: METRIC_LABEL[key],
          paise: value === undefined ? null : value,
          sub: METRIC_DEFINITION[key],
          accent: key === 'available' ? 'safe' : key === 'received_not_billed' ? 'watch' : 'info',
          metric: key,
          href: drilldownHref(METRIC_TARGET[key] || 'analytics-wbs-element', filters, {
            metric: key, dimension: 'wbs',
          }),
        }));
        /* THE ROUND TRIP, AND THE ONE SUBTLETY A WBS TABLE ADDS.
           The project total is checked against the ROOT rows only, never
           against every row: each node's figure already contains its
           descendants, so summing all rows would double count every ancestor
           and the check would fail on a perfectly correct hierarchy. Checking
           the roots is the check that actually means something — the project
           total is the sum of its top-level branches and nothing else. */
        const check = assertSumsBack(value, roots, key);
        check.label = `${METRIC_LABEL[key]} · project total versus its ${roots.length} root `
          + 'element(s). Only the roots are summed: every node already contains its descendants, '
          + 'so summing all rows would double count every ancestor.';
        checks.push(check);
      }
      band.appendChild(countCard({
        label: 'WBS elements',
        count: rows.length,
        sub: 'in this hierarchy, at every level',
        metric: 'wbs_count',
      }));
      tiles.appendChild(band);

      const failed = checks.filter((c) => !c.ok);
      if (failed.length) {
        for (const check of failed) quality.appendChild(reconciliationBlock(check));
      } else if (checks.length) {
        quality.appendChild(reconciliationBlock({
          ok: true,
          checked: true,
          message: `All ${checks.length} project totals are exactly the sum of the `
            + `${roots.length} root element(s) beneath them.`,
        }));
      }
    }

    function renderPrompt() {
      while (prompt.firstChild) prompt.removeChild(prompt.firstChild);
      prompt.appendChild(h('div', { class: 'msg msg-info', role: 'status', 'data-state': 'unstarted' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
        h('div', { class: 'body' }, [
          h('strong', {}, 'Choose a CAPEX project to load its WBS hierarchy.'),
          h('div', {}, [
            text('This screen reads one project at a time. Nothing has been asked for yet, so '
              + 'nothing is shown — this is not an empty hierarchy and not a failure. '),
            h('a', { class: 'linkish', href: screenHref('analytics-project-list', filters) },
              'Pick one from the project list'),
            text(', or type a project id into the filter above.'),
          ]),
        ]),
      ]));
    }

    async function wireExport() {
      const availability = await exportAvailable();
      while (exportHost.firstChild) exportHost.removeChild(exportHost.firstChild);
      exportHost.appendChild(exportButton({
        availability,
        report: spec.report,
        onExport: async () => {
          const result = await queueExport(spec.report, filters);
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
        // UNSTARTED. Not loading, not empty, not an error. The loader is left
        // hidden and the prompt carries the explanation.
        renderPrompt();
        loader.host.set('ready');
        loader.el.setAttribute('data-analytics-state', 'unstarted');
        tree.setRows([]);
        tree.el.hidden = true;
        while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
        controls.hidden = true;
        announce('Choose a CAPEX project to load its WBS hierarchy.');
        return;
      }
      while (prompt.firstChild) prompt.removeChild(prompt.firstChild);
      controls.hidden = false;
      loading = true;
      tree.el.hidden = false;
      tree.renderSkeleton();

      await loader.run(() => getWbs(projectId, filters, spec.report), {
        render: (data, result) => {
          renderChips(result.unapplied);
          const nested = Array.isArray(data && data.tree) ? data.tree
            : Array.isArray(data && data.rows) ? data.rows
              : Array.isArray(data) ? data : null;
          if (nested === null) {
            throw new Error('The WBS response carried no recognisable tree, so this screen '
              + 'cannot tell an empty hierarchy from a response it could not read.');
          }
          const { rows, truncated } = flattenWbs(nested);
          const totals = readTotals(data);
          renderCards(totals, rows);
          if (truncated) {
            quality.appendChild(h('div', { class: 'msg msg-warning', role: 'alert' }, [
              h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
              h('div', { class: 'body' }, 'This hierarchy is deeper than the supported limit and '
                + 'was truncated for display. The figures above are the server\'s and are '
                + 'complete; the ROWS below are not, and must not be read as the whole tree.'),
            ]));
          }
          if (!rows.length) { tree.setRows([]); tree.el.hidden = true; return false; }
          // See executive-dashboard.js: `onState('loading')` hides this, and
          // only the success path can put it back.
          tree.el.hidden = false;
          tree.setRows(rows, { collapseBelow: spec.collapseBelow });
          announceCount();
          return true;
        },
        onState: (state) => {
          if (state !== 'ready' && state !== 'stale') {
            tree.setRows([]);
            tree.el.hidden = true;
            controls.hidden = true;
            while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
          }
        },
      });

      loading = false;
    }

    wireExport();
    load();
  };
}
