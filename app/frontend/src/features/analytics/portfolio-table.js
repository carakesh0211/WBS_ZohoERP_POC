/* app/frontend/src/features/analytics/portfolio-table.js
   The project table SCR-01, SCR-02 and SCR-04 all render — built once.

   THREE SCREENS, ONE TABLE, AND WHY THAT IS NOT LAZINESS
   ------------------------------------------------------
   The executive dashboard, the controller workbench and the project list ask
   different questions of the same rows, and the temptation is three tables
   with three column sets. The reason they share one builder is not brevity: it
   is that a column's DEFINITION must be identical on all three. "Open
   commitment" on the executive dashboard and "Open commitment" in the project
   list have to be ordered-less-billed on both, or a controller who reconciles
   one against the other is reconciling two different quantities that carry the
   same heading. `METRIC_DEFINITION` is attached to every heading as a title
   for the same reason.

   The screens differ in WHICH columns they show, which the `metrics` argument
   selects, and in what the row link does.

   EVERY MONEY CELL IS DRILLABLE
   -----------------------------
   The Wave 7 contract: "Drill-down from EVERY metric to its source records."
   That applies to the cells as well as the cards — a controller who spots an
   odd Actual CWIP against one project should reach that project's bills from
   the number itself. Each money cell is a link carrying the SAME FilterSet
   plus this row's project, built by `drilldownHref()` so it cannot diverge
   from the card above it.
*/

import { h, text, setGeometry } from '../../core/dom.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { inr } from './analytics-metrics.js';
import { METRIC_DEFINITION, METRIC_LABEL } from './analytics-shapes.js';
import { bandBar, bandChip } from './analytics-kit.js';
import { drilldownHref } from './analytics-filters.js';

/** Which screen a given metric's drill-down opens. */
export const METRIC_TARGET = Object.freeze({
  budget: 'analytics-wbs-tree',
  original: 'analytics-wbs-tree',
  revisions: 'analytics-wbs-tree',
  ordered: 'analytics-commitment-ageing',
  commitment: 'analytics-commitment-ageing',
  actual: 'analytics-cwip-ledger',
  received: 'analytics-cwip-ledger',
  received_not_billed: 'analytics-commitment-ageing',
  pr_reserved: 'analytics-commitment-ageing',
  exposure: 'analytics-project-object',
  available: 'analytics-wbs-tree',
});

/**
 * A money cell that is also the drill-down into the records behind it.
 *
 * A `null` value renders as an explicit "not reported" rather than as a dash:
 * on a table where other rows DO carry a value, a dash reads as "this one has
 * none", which is a different and false claim. The same reasoning the
 * integration feature applied to its unmeasurable columns.
 */
function moneyCell(row, key, filters) {
  const value = row[key];
  if (value === null || value === undefined) {
    return h('span', {
      class: 'analytics-not-reported',
      title: `The source that answered this screen did not report ${METRIC_LABEL[key] || key} for `
        + 'this row. It is not zero.',
    }, [h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not reported')]);
  }
  if (!row.project_id) return text(inr(value));
  return h('a', {
    class: 'linkish analytics-drill',
    'data-metric': key,
    href: drilldownHref(METRIC_TARGET[key] || 'analytics-project-object', filters, {
      metric: key,
      dimension: 'project',
      narrowKey: 'project_ids',
      narrowValue: row.project_id,
    }),
    title: `${inr(value)} — open the source records behind ${METRIC_LABEL[key] || key} for `
      + `${row.capex_code || row.project_id}. The drill-down carries the same filters as this `
      + 'screen plus this project.',
  }, inr(value));
}

/**
 * Build the project table.
 *
 * @param {Object} config
 * @param {string[]} config.metrics - metric keys, as columns, in order.
 * @param {Object} config.filters - the CURRENT FilterSet. Read at build time
 *   through a getter so the table always links with the live filters rather
 *   than the ones present when it was constructed.
 * @param {()=>Object} config.getFilters
 * @param {string} config.caption
 * @param {string} [config.rowTarget] - the screen a row's code opens.
 * @param {boolean} [config.showBand]
 */
export function createProjectTable({
  metrics, getFilters, caption, rowTarget = 'analytics-project-object', showBand = true,
}) {
  const columns = [
    {
      key: 'capex_code',
      label: 'CAPEX code',
      render: (r) => (r.project_id
        ? h('a', {
          class: 'linkish mono',
          href: drilldownHref(rowTarget, getFilters(), {
            dimension: 'project', narrowKey: 'project_ids', narrowValue: r.project_id,
          }),
          title: `Open ${r.name || r.capex_code || r.project_id}.`,
        }, String(r.capex_code || r.project_id))
        : h('span', { class: 'mono muted' }, String(r.capex_code || '—'))),
    },
    { key: 'name', label: 'Project', render: (r) => text(r.name || '—') },
    { key: 'entity', label: 'Entity', render: (r) => text(r.entity || '—') },
    { key: 'plant', label: 'Plant', render: (r) => text(r.plant || '—') },
    {
      key: 'status',
      label: 'Status',
      render: (r) => (r.status
        ? statusChip({ label: String(r.status), tone: 'neutral', title: `Lifecycle status: ${r.status}.` })
        : h('span', { class: 'muted' }, '—')),
    },
  ];

  for (const key of metrics) {
    columns.push({
      key,
      label: METRIC_LABEL[key] || key,
      numeric: true,
      render: (r) => moneyCell(r, key, getFilters()),
    });
  }

  if (showBand) {
    columns.push({
      key: 'utilisation_bp',
      label: 'Utilisation',
      render: (r) => h('div', { class: 'analytics-band-cell' }, [
        bandChip(r.utilisation_bp),
        bandBar({
          actualBp: r.actual_bp,
          commitmentBp: r.utilisation_bp,
          label: `Exposure against budget for ${r.capex_code || r.project_id || 'this project'}.`,
        }),
      ]),
    });
  }

  const table = createDataTable({
    caption,
    emptyMessage: 'No project matched these filters.',
    columns,
  });

  /* Column headings carry the metric DEFINITION as a title, so the difference
     between "Ordered" and "Open commitment" is available at the point a reader
     is deciding which one they are looking at. */
  for (const key of metrics) {
    const th = [...table.el.querySelectorAll('thead th')]
      .find((cell) => cell.textContent.trim() === (METRIC_LABEL[key] || key));
    if (th && METRIC_DEFINITION[key]) th.setAttribute('title', METRIC_DEFINITION[key]);
  }

  return table;
}

/**
 * The totals footer, rendered from the SERVER's totals block.
 *
 * Never summed from the rows on screen. A footer computed in the browser is
 * the total of the page, and a page total under a heading that says
 * "Portfolio total" is the specific lie this feature is built to avoid.
 * `assertSumsBack()` compares the two and REPORTS a difference; it does not
 * substitute one for the other.
 */
export function totalsFooter(table, totals, metrics, { showBand = true, leading = 5 } = {}) {
  const tfoot = h('tfoot');
  const tr = h('tr');
  tr.appendChild(h('th', { scope: 'row', colspan: String(leading) }, 'Total, as the server computed it'));
  for (const key of metrics) {
    const value = totals ? totals[key] : null;
    tr.appendChild(h('td', { class: 'num' }, value === null || value === undefined
      ? h('span', { class: 'analytics-not-reported' }, [
        h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not reported'),
      ])
      : text(inr(value))));
  }
  if (showBand) tr.appendChild(h('td', {}, ''));
  tfoot.appendChild(tr);
  const existing = table.el.querySelector('tfoot');
  if (existing) existing.remove();
  table.el.querySelector('table').appendChild(tfoot);
  return tfoot;
}

/**
 * A horizontal composition bar for one project: actual, then commitment, then
 * whatever remains available. Geometry through the CSSOM, widths from integer
 * basis points.
 */
export function compositionBar(row) {
  return bandBar({
    actualBp: row.actual_bp,
    commitmentBp: row.utilisation_bp,
    label: `Actual CWIP and open commitment against budget for ${row.capex_code || 'this project'}.`,
  });
}

/** Set an indent width through the CSSOM. Never a style attribute. */
export function indent(el, depth) {
  setGeometry(el, { width: `${Math.max(0, Math.trunc(depth)) * 14}px` });
  return el;
}
