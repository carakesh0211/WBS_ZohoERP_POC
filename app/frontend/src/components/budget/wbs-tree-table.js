/* app/frontend/src/components/budget/wbs-tree-table.js
   SCR-09 Budget Planning Grid — a WBS tree table where each row is one
   (wbs_id, budget_head_id) cell from GET /api/budget/cells. Reuses the
   approved tree-table classes styles.css already carries for exactly this
   purpose (.tree-toggle, .wbs-cell, .wbs-code, .bar / .bar.breach / i.commit
   / i.actual, .col-bar, .col-wbs) and sets only geometry (indentation, bar
   segment widths) through the CSSOM via core/dom.js::setGeometry, never a
   style="" attribute — see ui-contract.md "The CSP constraint is
   load-bearing".

   Money columns use core/format.js::formatINR verbatim (integer paise in,
   INR string out) and are never computed here: budget_paise, commitment_paise,
   actual_paise, received_not_billed_paise, pr_reserved_paise, available_paise
   and exposure_paise all come straight from the API response, because
   docs/WAVE2_CONTRACTS.md is explicit that these are server-computed subtree
   sums over wbs_path — recomputing them client-side would silently disagree
   with the server the moment a filter narrows the tree.
*/

import { h, text, clear, setGeometry } from '../../core/dom.js';
import { formatINR } from '../../core/format.js';
import { pathsWithChildren } from './wbs-hierarchy.js';

const SKELETON_ROWS = 6;
const INDENT_PX = 18;
const COLUMN_COUNT = 10; // toggle+wbs, head, budget, commitment, actual, rnb, pr-reserved, available, util, actions

/**
 * @param {Object} opts
 * @param {(row:Object)=>Promise<Node>} opts.loadDrillContent - called the
 *   first time a row's "View lines" control is expanded; must resolve to a
 *   Node to place in the detail row (its own loading/error handling is the
 *   caller's responsibility, e.g. return a placeholder immediately and mutate
 *   it once the fetch resolves — see budget-grid.js).
 */
export function createWbsTreeTable({ loadDrillContent } = {}) {
  const collapsed = new Set(); // wbs_path values whose descendants are hidden
  let currentRows = [];

  const headRow = h('tr', {}, [
    h('th', { scope: 'col', class: 'col-wbs' }, 'WBS'),
    h('th', { scope: 'col' }, 'Budget Head'),
    h('th', { scope: 'col', class: 'num' }, 'Budget'),
    h('th', { scope: 'col', class: 'num' }, 'Commitment'),
    h('th', { scope: 'col', class: 'num' }, 'Actual'),
    h('th', { scope: 'col', class: 'num' }, 'Received, Not Billed'),
    h('th', { scope: 'col', class: 'num' }, 'PR Reserved'),
    h('th', { scope: 'col', class: 'num' }, 'Available'),
    h('th', { scope: 'col', class: 'col-bar' }, 'Utilisation'),
    h('th', { scope: 'col' }, [h('span', { class: 'sr-only' }, 'Row actions')]),
  ]);
  const thead = h('thead', {}, headRow);
  const tbody = h('tbody');
  const caption = h('caption', { class: 'sr-only' },
    'Budget planning grid: WBS element by budget head, with budget, commitment, actual, received-not-billed, PR reservation and available amounts');
  const table = h('table', {}, [caption, thead, tbody]);
  const wrap = h('div', { class: 'table-wrap', tabindex: '0' }, table);

  function renderSkeleton() {
    clear(tbody);
    for (let i = 0; i < SKELETON_ROWS; i += 1) {
      const tr = h('tr', { class: 'audit-skel-row', 'aria-hidden': 'true' });
      for (let c = 0; c < COLUMN_COUNT; c += 1) {
        const bar = h('span', { class: 'audit-skel-bar' });
        setGeometry(bar, { width: `${30 + ((i * 11 + c * 9) % 55)}%` });
        tr.appendChild(h('td', {}, bar));
      }
      tbody.appendChild(tr);
    }
  }

  function renderEmpty(message) {
    clear(tbody);
    tbody.appendChild(h('tr', {}, h('td', { colspan: String(COLUMN_COUNT) }, [
      h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '▦'),
        h('div', {}, message),
      ]),
    ])));
  }

  function utilisationBar(row) {
    const budget = Number(row.budget_paise) || 0;
    const exposure = Number(row.exposure_paise) || 0;
    if (!budget) return h('span', { class: 'muted small' }, 'No budget');
    const commit = Number(row.commitment_paise) || 0;
    const actual = Number(row.actual_paise) || 0;
    const commitPct = Math.max(0, Math.min(100, (commit / budget) * 100));
    const actualPct = Math.max(0, Math.min(100, (actual / budget) * 100));
    const breach = exposure > budget;
    const label = `Actual ${formatINR(row.actual_paise)} · Commitment ${formatINR(row.commitment_paise)} `
      + `of ${formatINR(row.budget_paise)}${breach ? ' — over budget' : ''}`;
    const commitBar = h('i', { class: 'commit' });
    const actualBar = h('i', { class: 'actual' });
    setGeometry(commitBar, { left: `${actualPct}%`, width: `${commitPct}%` });
    setGeometry(actualBar, { left: '0%', width: `${actualPct}%` });
    return h('div', { class: `bar${breach ? ' breach' : ''}`, role: 'img', 'aria-label': label, title: label },
      [commitBar, actualBar]);
  }

  function isVisible(row) {
    let p = row._parentPath;
    while (p) {
      if (collapsed.has(p)) return false;
      const idx = p.lastIndexOf('.');
      p = idx === -1 ? null : p.slice(0, idx);
    }
    return true;
  }

  function renderRows(rows) {
    currentRows = rows || [];
    clear(tbody);
    if (!currentRows.length) { renderEmpty('No budget cells match these filters.'); return; }

    const withChildren = pathsWithChildren(currentRows);
    const seenPathToggle = new Set(); // only the first row for a path gets the toggle control

    for (const row of currentRows) {
      const tr = h('tr', row._depth === 0 ? { class: 'lvl-1' } : {});
      if (!isVisible(row)) tr.hidden = true;

      const path = String(row.wbs_path || '');
      const hasChildren = withChildren.has(path);
      const isFirstForPath = !seenPathToggle.has(path);
      if (isFirstForPath) seenPathToggle.add(path);

      let toggleEl;
      if (hasChildren && isFirstForPath) {
        const isOpen = !collapsed.has(path);
        toggleEl = h('button', {
          type: 'button',
          class: 'tree-toggle',
          'aria-expanded': String(isOpen),
          'aria-label': `${isOpen ? 'Collapse' : 'Expand'} ${path || row.wbs_id}`,
          onClick: () => {
            if (collapsed.has(path)) collapsed.delete(path); else collapsed.add(path);
            renderRows(currentRows);
          },
        }, isOpen ? '▾' : '▸');
      } else {
        toggleEl = h('span', { class: 'tree-toggle leaf', 'aria-hidden': 'true' });
      }

      const wbsCell = h('th', { scope: 'row', class: 'wbs-cell' }, [
        toggleEl,
        h('span', { class: 'wbs-code' }, path || row.wbs_id || '—'),
        row.wbs_id && row.wbs_id !== path
          ? h('div', { class: 'muted xs mono' }, String(row.wbs_id))
          : null,
      ].filter(Boolean));
      setGeometry(wbsCell, { paddingLeft: `${8 + (row._depth || 0) * INDENT_PX}px` });
      tr.appendChild(wbsCell);

      tr.appendChild(h('td', {}, text(row.budget_head_id || '—')));
      tr.appendChild(h('td', { class: 'num' }, text(formatINR(row.budget_paise))));
      tr.appendChild(h('td', { class: 'num' }, text(formatINR(row.commitment_paise))));
      tr.appendChild(h('td', { class: 'num' }, text(formatINR(row.actual_paise))));
      tr.appendChild(h('td', { class: 'num' }, text(formatINR(row.received_not_billed_paise))));
      tr.appendChild(h('td', { class: 'num' }, text(formatINR(row.pr_reserved_paise))));
      const availTd = h('td', { class: 'num' }, text(formatINR(row.available_paise)));
      if (Number(row.available_paise) < 0) availTd.classList.add('neg');
      tr.appendChild(availTd);
      tr.appendChild(h('td', { class: 'col-bar' }, utilisationBar(row)));

      const detailRow = h('tr', { hidden: true });
      const detailTd = h('td', { colspan: String(COLUMN_COUNT) });
      const drillBtn = h('button', {
        type: 'button',
        class: 'btn-sm linkish',
        'aria-expanded': 'false',
        onClick: async () => {
          const expanded = drillBtn.getAttribute('aria-expanded') === 'true';
          drillBtn.setAttribute('aria-expanded', expanded ? 'false' : 'true');
          drillBtn.textContent = expanded ? 'View lines' : 'Hide lines';
          detailRow.hidden = expanded;
          if (!expanded && loadDrillContent) {
            clear(detailTd);
            detailTd.appendChild(h('div', { class: 'loading' }, 'Loading budget lines…'));
            try {
              const content = await loadDrillContent(row);
              clear(detailTd);
              if (content) detailTd.appendChild(content);
            } catch {
              clear(detailTd);
              detailTd.appendChild(h('div', { class: 'msg msg-error', role: 'alert' }, [
                h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
                h('div', { class: 'body' }, 'The budget lines for this cell could not be loaded.'),
              ]));
            }
          }
        },
      }, 'View lines');
      tr.appendChild(h('td', {}, drillBtn));
      detailRow.appendChild(detailTd);
      if (!isVisible(row)) detailRow.hidden = true;

      tbody.appendChild(tr);
      tbody.appendChild(detailRow);
    }
  }

  return { el: wrap, renderSkeleton, renderEmpty, renderRows };
}
