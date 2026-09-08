/* app/frontend/src/features/analytics/wbs-element-detail.js
   SCR-08 — WBS Element Detail Page.

   Named verbatim in research/30_contracts/C8_screens.json as "WBS Element
   Detail Page", so this screen carries its real number.

   THE ONE SCREEN WHERE OWN AND ROLLUP MUST BOTH BE VISIBLE
   ---------------------------------------------------------
   Everywhere else in this feature a node shows its ROLLUP — its own value plus
   everything beneath it — because that is what a hierarchy table is for. Here,
   on one element, the two are separated and both are shown, because this is
   the only screen where the difference can be inspected:

     OWN     what was posted against this element itself.
     ROLLUP  own plus every descendant.

   A reader who takes the rollup for the own-value over-attributes spend to a
   parent; a reader who takes the own-value for the rollup under-states a
   branch. Showing one and calling it "the" figure is how both mistakes happen,
   so neither is shown alone and the difference between them is labelled.

   A LEAF HAS NO DIFFERENCE, AND THAT IS SAID
   ------------------------------------------
   On an element with no children the two are identical, and the screen says
   so rather than rendering the same number twice with no explanation — two
   identical numbers side by side under different headings reads as a bug.

   THE ELEMENT IS FOUND, NOT FETCHED
   ---------------------------------
   There is no `/api/wbs/{id}` in this build. The element is located inside the
   project's tree, which is a real read of a real route — and when the element
   is not in that tree, the screen says the ELEMENT was not found in THIS
   PROJECT, which is a different sentence from "you may not see it" and from
   "the route is missing". A1's reporting service may later answer an element
   directly; `REPORT_IDS.wbsElement` is already the preferred candidate, so
   that lands without a change here.
*/

import { h, text } from '../../core/dom.js';
import { createLoader, requireRoot } from './analytics-screen.js';
import {
  card, createAnnouncer, exportButton, keyValues, metricCard, reconciliationBlock,
} from './analytics-kit.js';
import {
  createFilterBar, drilldownHref, filterChips, readFilters, screenHref, writeFilters,
} from './analytics-filters.js';
import { assertSumsBack, inr } from './analytics-metrics.js';
import {
  flattenWbs, METRIC_DEFINITION, METRIC_LABEL, METRICS, metric as readMetric,
} from './analytics-shapes.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { METRIC_TARGET } from './portfolio-table.js';
import {
  exportAvailable, getWbs, queueExport, REPORT_IDS,
} from './analytics-api.js';

const CARDS = ['budget', 'commitment', 'actual', 'received_not_billed', 'available'];

/**
 * The DIRECT children of `row`, from the flattened depth-first list.
 *
 * `flattenWbs()` emits pre-order, so a node's descendants are exactly the rows
 * that follow it until one appears at its own depth or shallower. Of those,
 * the direct children are the ones one level deeper.
 *
 * Written as a scan rather than as a filter over the whole list because the
 * "one level deeper" test alone is not enough: a NEPHEW is also one level
 * deeper than its uncle, and including nephews would make the rollup identity
 * fail on a perfectly correct hierarchy — which is exactly the double-counting
 * class of bug this feature is written against, arriving from the other
 * direction.
 */
function directChildren(rows, row) {
  const start = rows.indexOf(row);
  if (start === -1) return [];
  const out = [];
  for (let i = start + 1; i < rows.length; i += 1) {
    if (rows[i].depth <= row.depth) break;
    if (rows[i].depth === row.depth + 1) out.push(rows[i]);
  }
  return out;
}

export function mountWbsElementDetail(root) {
  requireRoot(root, 'SCR-08 WBS Element Detail Page');
  const announce = createAnnouncer('analyticsLiveRegion');

  let filters = readFilters();
  const getFilters = () => filters;
  const projectOf = () => (filters.project_ids && filters.project_ids.length
    ? filters.project_ids[0] : null);
  const wbsOf = () => (filters.wbs_paths && filters.wbs_paths.length ? filters.wbs_paths[0] : null);

  const chips = h('div', { class: 'analytics-chip-host' });
  const prompt = h('div', { class: 'analytics-prompt' });
  const identity = h('div', { class: 'analytics-identity' });
  const tiles = h('div', { id: 'elemTiles' });
  const compare = h('div', { class: 'analytics-compare' });
  const quality = h('div', { class: 'analytics-quality-host' });
  const exportHost = h('span', { class: 'analytics-export-host' });

  const children = createDataTable({
    caption: 'The direct children of this WBS element, each showing its own rollup',
    emptyMessage: 'This element has no child elements — it is a leaf, and everything posted '
      + 'against it is its own.',
    columns: [
      {
        key: 'wbs_code',
        label: 'WBS element',
        render: (r) => h('a', {
          class: 'linkish mono',
          href: drilldownHref('analytics-wbs-element', getFilters(), {
            dimension: 'wbs', narrowKey: 'wbs_paths', narrowValue: r.wbs_code,
          }),
        }, String(r.wbs_code || '—')),
      },
      { key: 'description', label: 'Description', render: (r) => text(r.description || '—') },
      {
        key: 'carries_budget',
        label: 'Holds budget',
        render: (r) => statusChip({
          label: r.carries_budget ? 'OWN BUDGET' : 'ROLLUP',
          tone: r.carries_budget ? 'positive' : 'neutral',
          title: r.carries_budget
            ? 'This child holds an approved budget of its own.'
            : 'This child holds no budget of its own; its figures are the rollup of everything '
              + 'beneath it.',
        }),
      },
      ...['budget', 'commitment', 'actual', 'available'].map((key) => ({
        key,
        label: METRIC_LABEL[key],
        numeric: true,
        render: (r) => (r[key] === null || r[key] === undefined
          ? h('span', { class: 'analytics-not-reported' }, [
            h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not reported')])
          : text(inr(r[key]))),
      })),
    ],
  });

  const filterBar = createFilterBar({
    idPrefix: 'elem',
    fields: ['project_ids', 'wbs_paths', 'date_from', 'date_to', 'period_ids'],
    value: filters,
    onApply: (next) => apply(next),
  });

  const loader = createLoader({
    id: 'elemStatus',
    glyph: '⌗',
    what: 'This WBS element',
    emptyMessage: 'No WBS element matched.',
    loadingMessage: 'Loading the WBS element…',
    onRetry: () => load(),
    announce,
  });

  root.appendChild(card('elemTitle', 'WBS Element Detail', [
    h('p', { class: 'muted small' },
      'Own value and rollup are shown separately here, and only here. Everywhere else a node '
      + 'shows its rollup, because that is what a hierarchy table is for — but taking a rollup for '
      + 'an own-value over-attributes spend to a parent, and taking an own-value for a rollup '
      + 'under-states a branch. Both are labelled below.'),
    filterBar.el,
    chips,
    prompt,
    loader.el,
    identity,
    tiles,
    compare,
    quality,
    h('h3', { class: 'analytics-subhead' }, 'Child elements'),
    children.el,
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
    return h('nav', { class: 'analytics-crosslinks', 'aria-label': 'Related views for this element' }, [
      h('span', { class: 'muted' }, 'This element in:'),
      h('a', { class: 'linkish', href: screenHref('analytics-wbs-tree', filters) }, 'the WBS tree table'),
      h('a', { class: 'linkish', href: screenHref('analytics-cwip-ledger', filters) }, 'the CWIP ledger'),
      h('a', { class: 'linkish', href: screenHref('analytics-commitment-ageing', filters) },
        'open commitment ageing'),
    ]);
  }

  function renderPrompt(message) {
    while (prompt.firstChild) prompt.removeChild(prompt.firstChild);
    prompt.appendChild(h('div', { class: 'msg msg-info', role: 'status', 'data-state': 'unstarted' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, message),
        h('div', {}, [
          text('Nothing has been asked for yet, so nothing is shown. '),
          h('a', { class: 'linkish', href: screenHref('analytics-wbs-tree', filters) },
            'Pick an element from the WBS tree table'),
          text('.'),
        ]),
      ]),
    ]));
  }

  function renderIdentity(row) {
    while (identity.firstChild) identity.removeChild(identity.firstChild);
    identity.appendChild(keyValues([
      ['WBS code', h('span', { class: 'mono' }, String(row.wbs_code || '—'))],
      ['Description', row.description ? text(String(row.description)) : null],
      ['Level', row.level !== null ? text(String(row.level)) : null],
      ['Budget head', row.budget_head ? text(String(row.budget_head)) : null],
      ['Asset category', row.asset_category ? text(String(row.asset_category)) : null],
      ['Responsible', row.responsible_user ? text(String(row.responsible_user)) : null],
      ['Planned start', row.planned_start ? text(String(row.planned_start)) : null],
      ['Planned end', row.planned_end ? text(String(row.planned_end)) : null],
      ['Status', row.status
        ? statusChip({ label: String(row.status), tone: 'neutral', title: `Lifecycle status: ${row.status}.` })
        : null],
      ['Holds budget', statusChip({
        label: row.carries_budget ? 'OWN BUDGET' : 'ROLLUP ONLY',
        tone: row.carries_budget ? 'positive' : 'neutral',
        title: row.carries_budget
          ? 'This element holds an approved budget of its own.'
          : row.budget_owner_code
            ? `This element holds no budget of its own; budget is held at ${row.budget_owner_code}.`
            : 'This element holds no budget of its own.',
      })],
      ['Procurement', statusChip({
        label: row.allow_procurement ? 'PERMITTED' : 'BLOCKED',
        tone: row.allow_procurement ? 'positive' : 'negative',
        title: 'Whether a purchase request may be raised against this element in its current '
          + 'lifecycle state.',
      })],
      ['Posting', statusChip({
        label: row.allow_posting ? 'PERMITTED' : 'BLOCKED',
        tone: row.allow_posting ? 'positive' : 'negative',
        title: 'Whether a bill may be posted against this element in its current lifecycle state.',
      })],
    ]));
  }

  /**
   * Own against rollup, side by side, for every one of the ten metrics.
   *
   * The two columns are the whole point of this screen, so they are a real
   * table with real headings rather than two card bands a reader has to
   * remember the order of.
   */
  function renderCompare(row) {
    while (compare.firstChild) compare.removeChild(compare.firstChild);
    const isLeaf = !row.has_children;
    const table = createDataTable({
      caption: 'Each metric for this element: what was posted against the element itself, and '
        + 'what the element carries once its descendants are included',
      columns: [
        {
          key: 'metric',
          label: 'Metric',
          render: (r) => h('span', { title: METRIC_DEFINITION[r.key] || '' }, METRIC_LABEL[r.key] || r.key),
        },
        {
          key: 'own',
          label: 'Own value',
          numeric: true,
          render: (r) => (r.own === null || r.own === undefined
            ? h('span', { class: 'analytics-not-reported' }, [
              h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not reported')])
            : text(inr(r.own))),
        },
        {
          key: 'rollup',
          label: 'Including descendants',
          numeric: true,
          render: (r) => (r.rollup === null || r.rollup === undefined
            ? h('span', { class: 'analytics-not-reported' }, [
              h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not reported')])
            : text(inr(r.rollup))),
        },
        {
          key: 'beneath',
          label: 'Held beneath this element',
          numeric: true,
          render: (r) => {
            if (r.own === null || r.rollup === null || r.own === undefined || r.rollup === undefined) {
              return h('span', {
                class: 'analytics-not-reported',
                title: 'One of the two figures was not reported, so the difference between them '
                  + 'cannot be stated. It is not zero.',
              }, [h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not measurable')]);
            }
            // Integer subtraction of two integer-paise values. The only
            // arithmetic on this screen, and it is exact.
            return text(inr(r.rollup - r.own));
          },
        },
      ],
      emptyMessage: 'No metric was reported for this element.',
    });
    const rows = [...METRICS, 'exposure'].map((key) => ({
      key,
      own: readMetric(row.own, key),
      rollup: row[key],
    }));
    table.renderRows(rows);
    compare.appendChild(h('section', { 'aria-labelledby': 'elemCompareTitle' }, [
      h('h3', { id: 'elemCompareTitle', class: 'analytics-subhead' }, 'Own value against rollup'),
      isLeaf
        ? h('p', { class: 'xs muted' },
          'This element is a leaf: it has no children, so its own value and its rollup are the '
          + 'same number. They are both shown so the two columns mean the same thing on every '
          + 'element, and the third column is zero here for that reason rather than by accident.')
        : h('p', { class: 'xs muted' },
          'The third column is the rollup less the own value: what is held beneath this element '
          + 'rather than against it.'),
      table.el,
    ]));
  }

  function renderCards(row) {
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
    while (quality.firstChild) quality.removeChild(quality.firstChild);
    const band = h('div', { class: 'tiles analytics-tiles' });
    for (const key of CARDS) {
      band.appendChild(metricCard({
        label: METRIC_LABEL[key],
        paise: row[key],
        sub: `${METRIC_DEFINITION[key]} Shown including descendants.`,
        accent: key === 'available' ? 'safe' : key === 'received_not_billed' ? 'watch' : 'info',
        metric: key,
        href: drilldownHref(METRIC_TARGET[key] || 'analytics-cwip-ledger', filters, {
          metric: key, dimension: 'wbs', narrowKey: 'wbs_paths', narrowValue: row.wbs_code,
        }),
      }));
    }
    tiles.appendChild(band);
  }

  function checkChildren(row, childRows) {
    while (quality.firstChild) quality.removeChild(quality.firstChild);
    if (!childRows.length) return;
    /* THE ROLLUP IDENTITY: this element's rollup must equal its OWN value plus
       the rollups of its DIRECT children. Checking against direct children
       only is the correct check — grandchildren are already inside the
       children's rollups, and including them would double count and fail on a
       correct hierarchy, which is the mistake this whole feature is written
       against. */
    for (const key of ['budget', 'commitment', 'actual']) {
      const own = readMetric(row.own, key);
      const rollup = row[key];
      if (own === null || rollup === null || own === undefined || rollup === undefined) continue;
      const check = assertSumsBack(rollup - own, childRows, key);
      check.label = `${METRIC_LABEL[key]} · this element's rollup less its own value, against the `
        + `${childRows.length} direct child element(s). Grandchildren are deliberately excluded: `
        + 'they are already inside their parents\' rollups.';
      quality.appendChild(reconciliationBlock(check));
    }
  }

  async function wireExport() {
    const availability = await exportAvailable();
    while (exportHost.firstChild) exportHost.removeChild(exportHost.firstChild);
    exportHost.appendChild(exportButton({
      availability,
      reportId: REPORT_IDS.wbsElement,
      onExport: async () => {
        const result = await queueExport(REPORT_IDS.wbsElement, filters);
        announce(result.queued ? 'The export has been queued.'
          : 'This build mounts no export endpoint, so nothing was queued.');
      },
    }));
  }

  function clearBody() {
    while (identity.firstChild) identity.removeChild(identity.firstChild);
    while (tiles.firstChild) tiles.removeChild(tiles.firstChild);
    while (compare.firstChild) compare.removeChild(compare.firstChild);
    children.renderRows([]);
    children.el.hidden = true;
  }

  let loading = false;

  async function load() {
    if (loading) return;
    renderChips([]);
    const projectId = projectOf();
    const wbsCode = wbsOf();
    if (!projectId || !wbsCode) {
      renderPrompt(projectId
        ? 'Choose a WBS element within this project.'
        : 'Choose a CAPEX project and a WBS element.');
      loader.host.set('ready');
      loader.el.setAttribute('data-analytics-state', 'unstarted');
      clearBody();
      announce('Choose a project and a WBS element to open its detail.');
      return;
    }
    while (prompt.firstChild) prompt.removeChild(prompt.firstChild);
    loading = true;
    children.el.hidden = false;
    children.renderSkeleton();

    await loader.run(() => getWbs(projectId, filters, REPORT_IDS.wbsElement), {
      render: (data, result) => {
        renderChips(result.unapplied);
        const nested = Array.isArray(data && data.tree) ? data.tree
          : Array.isArray(data && data.rows) ? data.rows
            : Array.isArray(data) ? data : null;
        if (nested === null) {
          throw new Error('The WBS response carried no recognisable tree, so this element could '
            + 'not be located in it.');
        }
        const { rows } = flattenWbs(nested);
        const row = rows.find((r) => r.wbs_code === wbsCode || r.wbs_id === wbsCode);
        if (!row) {
          /* NOT FOUND IN THIS PROJECT. Distinct from a refusal and distinct
             from an absent route, and worded so it names neither: it says
             what was searched and what was not in it. */
          clearBody();
          loader.notes.appendChild(h('div', { class: 'msg msg-info', role: 'status' }, [
            h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
            h('div', { class: 'body' }, [
              h('strong', {}, `${wbsCode} is not an element of ${projectId}.`),
              h('div', {}, `This project's hierarchy was read successfully and contains `
                + `${rows.length} element(s); none of them carries that code. Check the code, or `
                + 'open the tree table for this project and pick from it.'),
            ]),
          ]));
          return false;
        }
        const uniqueChildren = directChildren(rows, row);

        renderIdentity(row);
        renderCards(row);
        renderCompare(row);
        children.el.hidden = false;
        children.renderRows(uniqueChildren);
        checkChildren(row, uniqueChildren);
        announce(`${row.wbs_code}: ${uniqueChildren.length} direct child element(s).`);
        return true;
      },
      onState: (state) => {
        if (state !== 'ready' && state !== 'stale') clearBody();
      },
    });

    loading = false;
  }

  wireExport();
  load();
}
