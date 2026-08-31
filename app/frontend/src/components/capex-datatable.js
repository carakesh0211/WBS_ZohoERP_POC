/* app/frontend/src/components/capex-datatable.js
   Reusable data table: sortable columns, a loading skeleton, an empty state
   and per-row actions. Reuses the frozen table / .table-wrap / .status /
   .empty styling from styles.css; the only new rules it needs
   (.audit-skel-bar, th .sort-btn) live in extensions.css, tokens only.

   This is a plain factory, not a Web Component: no bundler, no custom-element
   registry, and it keeps every interpolation on the textContent / DOM-node
   path in core/dom.js rather than innerHTML.
*/

import { h, text, clear, setGeometry } from '../core/dom.js';

const SKELETON_ROWS = 6;

/**
 * @param {Object} opts
 * @param {Array<{key:string,label:string,numeric?:boolean,sortable?:boolean,
 *   render?:(row:Object)=>(Node|string)}>} opts.columns
 * @param {string} [opts.caption] - visually hidden table caption for screen readers.
 * @param {string} [opts.emptyMessage] - default empty-state copy.
 * @param {(key:string, direction:'asc'|'desc')=>void} [opts.onSort]
 * @param {(row:Object)=>(Node|null)} [opts.renderRowDetail] - optional expandable
 *   detail row content, toggled by a per-row "Details" button.
 */
export function createDataTable({ columns, caption, emptyMessage, onSort, renderRowDetail } = {}) {
  if (!Array.isArray(columns) || columns.length === 0) {
    throw new Error('createDataTable(): columns is required.');
  }

  const sortState = { key: null, direction: 'asc' };
  const headRow = h('tr');
  const headerCells = new Map();

  for (const col of columns) {
    const thAttrs = { scope: 'col' };
    if (col.numeric) thAttrs.class = 'num';
    let cellContent;
    if (col.sortable && onSort) {
      thAttrs['aria-sort'] = 'none';
      const sortBtn = h(
        'button',
        {
          type: 'button',
          class: 'sort-btn',
          onClick: () => {
            sortState.direction = sortState.key === col.key && sortState.direction === 'asc' ? 'desc' : 'asc';
            sortState.key = col.key;
            for (const [k, th] of headerCells) th.setAttribute('aria-sort', 'none');
            thHead.setAttribute('aria-sort', sortState.direction === 'asc' ? 'ascending' : 'descending');
            sortIco.textContent = sortState.direction === 'asc' ? '▲' : '▼';
            onSort(col.key, sortState.direction);
          },
        },
        [text(col.label), h('span', { class: 'sort-ico', 'aria-hidden': 'true' }, '')],
      );
      const sortIco = sortBtn.querySelector('.sort-ico');
      cellContent = sortBtn;
    } else {
      cellContent = text(col.label);
    }
    const thHead = h('th', thAttrs, cellContent);
    headerCells.set(col.key, thHead);
    headRow.appendChild(thHead);
  }
  if (renderRowDetail) headRow.appendChild(h('th', { scope: 'col' }, [h('span', { class: 'sr-only' }, 'Row actions')]));

  const thead = h('thead', {}, headRow);
  const tbody = h('tbody');
  const captionEl = caption ? h('caption', { class: 'sr-only' }, caption) : null;
  const table = h('table', {}, [captionEl, thead, tbody].filter(Boolean));
  const wrap = h('div', { class: 'table-wrap', tabindex: '0' }, table);

  const columnCount = columns.length + (renderRowDetail ? 1 : 0);

  function renderSkeleton() {
    clear(tbody);
    for (let i = 0; i < SKELETON_ROWS; i += 1) {
      const tr = h('tr', { class: 'audit-skel-row', 'aria-hidden': 'true' });
      for (const col of columns) {
        const bar = h('span', { class: 'audit-skel-bar' });
        setGeometry(bar, { width: `${35 + ((i * 13 + col.key.length * 7) % 55)}%` });
        tr.appendChild(h('td', col.numeric ? { class: 'num' } : {}, bar));
      }
      if (renderRowDetail) tr.appendChild(h('td', {}, ''));
      tbody.appendChild(tr);
    }
  }

  function renderEmpty(message) {
    clear(tbody);
    const td = h('td', { colspan: String(columnCount) }, [
      h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '⎙'),
        h('div', {}, message || emptyMessage || 'No rows to display.'),
      ]),
    ]);
    tbody.appendChild(h('tr', {}, td));
  }

  function renderRows(rows) {
    clear(tbody);
    if (!rows || rows.length === 0) { renderEmpty(); return; }
    for (const row of rows) {
      const tr = h('tr');
      for (const col of columns) {
        const cell = col.render ? col.render(row) : text(row[col.key]);
        tr.appendChild(h('td', col.numeric ? { class: 'num' } : {}, cell));
      }
      if (renderRowDetail) {
        const detailRow = h('tr', { hidden: true });
        const detailTd = h('td', { colspan: String(columnCount) });
        const detailBtn = h(
          'button',
          {
            type: 'button',
            class: 'btn-sm linkish',
            'aria-expanded': 'false',
            onClick: (ev) => {
              const expanded = detailBtn.getAttribute('aria-expanded') === 'true';
              detailBtn.setAttribute('aria-expanded', expanded ? 'false' : 'true');
              detailBtn.textContent = expanded ? 'Details' : 'Hide details';
              if (!expanded && detailTd.childNodes.length === 0) {
                const content = renderRowDetail(row);
                if (content) detailTd.appendChild(content);
              }
              detailRow.hidden = expanded;
            },
          },
          'Details',
        );
        tr.appendChild(h('td', {}, detailBtn));
        detailRow.appendChild(detailTd);
        tbody.appendChild(tr);
        tbody.appendChild(detailRow);
        continue;
      }
      tbody.appendChild(tr);
    }
  }

  return { el: wrap, renderSkeleton, renderEmpty, renderRows };
}
