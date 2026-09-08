/* app/frontend/src/features/analytics/wbs-tree.js
   The WBS tree table SCR-06 and SCR-07 both render.

   THE ONE THING A WBS TABLE MUST NOT GET WRONG
   --------------------------------------------
   A node's `total` is its OWN value plus everything beneath it. Its `own` is
   only what was posted against the node itself. A table that shows `total` on
   every row and then sums the rows DOUBLE COUNTS every ancestor — a two-level
   project reports its budget twice, a five-level one reports parts of it five
   times, and the total looks plausible at every level.

   This component never sums rows. It renders the rollup the server computed
   for each node, and the footer — where there is one — carries the SERVER's
   project total, which is the root rollup and not an addition performed here.
   `carries_budget` is rendered explicitly as a column so a reader can see
   which rows hold budget of their own and which are showing a rollup of their
   children's.

   INDENTATION GOES THROUGH THE CSSOM
   ----------------------------------
   `main.py` sets `style-src 'self'` with no `'unsafe-inline'`, so a markup
   `style="padding-left:28px"` is blocked outright and `core/dom.js::h()`
   throws on one. Depth is applied with `setGeometry()`, which CSP does not
   govern — the same mechanism the shell's existing `enhance()` uses for tree
   indentation and bar geometry.

   EXPAND AND COLLAPSE ARE REAL BUTTONS
   ------------------------------------
   Each parent's toggle is a `<button>` with `aria-expanded`, so it is
   reachable by keyboard, announced by a screen reader and carries a visible
   focus ring from the frozen stylesheet. The rows themselves carry
   `aria-level`, `aria-expanded` and `aria-posinset` so the hierarchy survives
   into the accessibility tree rather than existing only as visual indentation.
*/

import { h, text, clear, setGeometry } from '../../core/dom.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { inr } from './analytics-metrics.js';
import { METRIC_DEFINITION, METRIC_LABEL } from './analytics-shapes.js';
import { bandBar, bandChip } from './analytics-kit.js';
import { drilldownHref } from './analytics-filters.js';

const INDENT_PX = 14;

/**
 * Build the tree table.
 *
 * @param {Object} config
 * @param {string[]} config.metrics - money columns, in order.
 * @param {()=>Object} config.getFilters
 * @param {string} config.caption
 * @param {(row:Object)=>string} [config.rowHref] - where a WBS code links to.
 * @param {boolean} [config.showBand]
 */
export function createWbsTree({
  metrics, getFilters, caption, rowHref = null, showBand = true,
}) {
  const collapsed = new Set();
  let rows = [];

  const headRow = h('tr');
  headRow.appendChild(h('th', { scope: 'col', class: 'col-wbs' }, 'WBS element'));
  headRow.appendChild(h('th', { scope: 'col', class: 'col-desc' }, 'Description'));
  headRow.appendChild(h('th', { scope: 'col' }, 'Budget head'));
  headRow.appendChild(h('th', {
    scope: 'col',
    title: 'A node that carries budget holds an approved figure of its own. A node that does not '
      + 'is showing the rollup of everything beneath it — reading that as an own-value double '
      + 'counts every ancestor.',
  }, 'Holds budget'));
  headRow.appendChild(h('th', { scope: 'col' }, 'Status'));
  for (const key of metrics) {
    headRow.appendChild(h('th', {
      scope: 'col',
      class: 'num',
      title: METRIC_DEFINITION[key] || '',
    }, METRIC_LABEL[key] || key));
  }
  if (showBand) headRow.appendChild(h('th', { scope: 'col' }, 'Utilisation'));

  const tbody = h('tbody');
  const table = h('table', {}, [
    h('caption', { class: 'sr-only' }, caption),
    h('thead', {}, headRow),
    tbody,
  ]);
  const wrap = h('div', { class: 'table-wrap', tabindex: '0' }, table);
  const columnCount = 5 + metrics.length + (showBand ? 1 : 0);

  /** Is this row hidden because one of its ancestors is collapsed? */
  function hiddenByAncestor(index) {
    const row = rows[index];
    for (let i = index - 1; i >= 0; i -= 1) {
      const candidate = rows[i];
      if (candidate.depth < row.depth) {
        if (collapsed.has(candidate.wbs_id)) return true;
        if (candidate.depth === 0) return false;
      }
    }
    return false;
  }

  function moneyCell(row, key) {
    const value = row[key];
    if (value === null || value === undefined) {
      return h('span', {
        class: 'analytics-not-reported',
        title: `The source did not report ${METRIC_LABEL[key] || key} for this element. It is `
          + 'not zero.',
      }, [h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not reported')]);
    }
    const filters = getFilters();
    if (!row.wbs_code) return text(inr(value));
    return h('a', {
      class: 'linkish analytics-drill',
      'data-metric': key,
      href: drilldownHref(
        key === 'actual' || key === 'received' ? 'analytics-cwip-ledger'
          : key === 'commitment' || key === 'ordered' ? 'analytics-commitment-ageing'
            : 'analytics-wbs-element',
        filters,
        {
          metric: key,
          dimension: 'wbs',
          narrowKey: 'wbs_paths',
          narrowValue: row.wbs_code,
        },
      ),
      title: `${inr(value)} — open the source records behind ${METRIC_LABEL[key] || key} for `
        + `${row.wbs_code}. The drill-down carries this screen's filters plus this subtree.`,
    }, inr(value));
  }

  function renderRow(row, index, posinset, setsize) {
    const tr = h('tr', {
      class: 'analytics-tree-row',
      role: 'row',
      'aria-level': String(row.depth + 1),
      'aria-posinset': String(posinset),
      'aria-setsize': String(setsize),
      'data-wbs': row.wbs_code || '',
      'data-depth': String(row.depth),
      'data-carries-budget': String(row.carries_budget),
    });
    if (row.has_children) tr.setAttribute('aria-expanded', String(!collapsed.has(row.wbs_id)));

    const spacer = h('span', { class: 'analytics-tree-indent', 'aria-hidden': 'true' });
    // CSSOM, not a style attribute: the CSP blocks the attribute and h() throws.
    setGeometry(spacer, { width: `${row.depth * INDENT_PX}px` });

    const toggle = row.has_children
      ? h('button', {
        type: 'button',
        class: 'tree-toggle',
        'aria-expanded': String(!collapsed.has(row.wbs_id)),
        'aria-label': `${collapsed.has(row.wbs_id) ? 'Expand' : 'Collapse'} ${row.wbs_code || 'this element'}`,
        onClick: () => {
          if (collapsed.has(row.wbs_id)) collapsed.delete(row.wbs_id);
          else collapsed.add(row.wbs_id);
          paint();
        },
      }, collapsed.has(row.wbs_id) ? '▸' : '▾')
      : h('span', { class: 'tree-toggle leaf', 'aria-hidden': 'true' }, '▸');

    const code = rowHref && row.wbs_id
      ? h('a', { class: 'linkish mono analytics-tree-code', href: rowHref(row) }, String(row.wbs_code || row.wbs_id))
      : h('span', { class: 'mono analytics-tree-code' }, String(row.wbs_code || '—'));

    tr.appendChild(h('th', { scope: 'row', class: 'wbs-cell' },
      h('span', { class: 'analytics-tree-cell' }, [spacer, toggle, code])));
    tr.appendChild(h('td', { class: 'col-desc' }, text(row.description || '—')));
    tr.appendChild(h('td', {}, text(row.budget_head || '—')));
    tr.appendChild(h('td', {}, row.carries_budget
      ? statusChip({
        label: 'OWN BUDGET',
        tone: 'positive',
        title: 'This element holds an approved budget of its own.',
      })
      : statusChip({
        label: 'ROLLUP',
        tone: 'neutral',
        title: row.budget_owner_code
          ? `This element holds no budget of its own; budget is held at ${row.budget_owner_code}. `
            + 'The figures on this row are the rollup of everything beneath it.'
          : 'This element holds no budget of its own. The figures on this row are the rollup of '
            + 'everything beneath it — adding them to its children would double count.',
      })));
    tr.appendChild(h('td', {}, row.status
      ? statusChip({ label: String(row.status), tone: 'neutral', title: `Lifecycle status: ${row.status}.` })
      : h('span', { class: 'muted' }, '—')));
    for (const key of metrics) tr.appendChild(h('td', { class: 'num' }, moneyCell(row, key)));
    if (showBand) {
      tr.appendChild(h('td', {}, h('div', { class: 'analytics-band-cell' }, [
        bandChip(row.utilisation_bp),
        bandBar({
          actualBp: row.actual_bp,
          commitmentBp: row.utilisation_bp,
          label: `Exposure against budget for ${row.wbs_code || 'this element'}.`,
        }),
      ])));
    }
    return tr;
  }

  function paint() {
    clear(tbody);
    if (!rows.length) {
      tbody.appendChild(h('tr', {}, h('td', { colspan: String(columnCount) },
        h('div', { class: 'empty' }, [
          h('div', { class: 'big', 'aria-hidden': 'true' }, '⌗'),
          h('div', {}, 'This project has no WBS element matching these filters.'),
        ]))));
      return;
    }
    /* Sibling counts, for aria-posinset / aria-setsize. Computed per parent so
       a screen reader can say "3 of 7" rather than "3 of the whole tree". */
    const siblingIndex = new Map();
    const siblingCount = new Map();
    for (const row of rows) {
      const key = `${row.depth}:${row.parentKey || ''}`;
      siblingCount.set(key, (siblingCount.get(key) || 0) + 1);
    }
    rows.forEach((row, index) => {
      if (hiddenByAncestor(index)) return;
      const key = `${row.depth}:${row.parentKey || ''}`;
      const pos = (siblingIndex.get(key) || 0) + 1;
      siblingIndex.set(key, pos);
      tbody.appendChild(renderRow(row, index, pos, siblingCount.get(key) || 1));
    });
  }

  /** Skeleton rows, matching the shared table component's treatment. */
  function renderSkeleton() {
    clear(tbody);
    for (let i = 0; i < 6; i += 1) {
      const tr = h('tr', { class: 'audit-skel-row', 'aria-hidden': 'true' });
      for (let c = 0; c < columnCount; c += 1) {
        const bar = h('span', { class: 'audit-skel-bar' });
        setGeometry(bar, { width: `${35 + ((i * 13 + c * 7) % 55)}%` });
        tr.appendChild(h('td', {}, bar));
      }
      tbody.appendChild(tr);
    }
  }

  /**
   * @param {Array} next - rows from `flattenWbs()`.
   * @param {Object} [opts]
   * @param {number} [opts.collapseBelow] - collapse every node deeper than
   *   this. An explorer opens shallow; a tree table opens flat.
   */
  function setRows(next, { collapseBelow = null } = {}) {
    rows = (next || []).map((row, index, all) => {
      // Remember the parent's code, so sibling counting does not need a second
      // walk of the nested structure.
      let parentKey = '';
      for (let i = index - 1; i >= 0; i -= 1) {
        if (all[i].depth < row.depth) { parentKey = all[i].wbs_code || all[i].wbs_id || ''; break; }
      }
      return { ...row, parentKey };
    });
    collapsed.clear();
    if (collapseBelow !== null) {
      for (const row of rows) {
        if (row.has_children && row.depth >= collapseBelow) collapsed.add(row.wbs_id);
      }
    }
    paint();
  }

  function expandAll() { collapsed.clear(); paint(); }

  function collapseAll(depth = 0) {
    collapsed.clear();
    for (const row of rows) if (row.has_children && row.depth >= depth) collapsed.add(row.wbs_id);
    paint();
  }

  /** How many rows are currently visible — for the announcer, not for a total. */
  function visibleCount() {
    return rows.filter((_, index) => !hiddenByAncestor(index)).length;
  }

  return { el: wrap, setRows, renderSkeleton, expandAll, collapseAll, visibleCount, paint };
}
