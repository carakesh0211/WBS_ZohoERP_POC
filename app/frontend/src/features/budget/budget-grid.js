/* app/frontend/src/features/budget/budget-grid.js
   SCR-09 Budget Planning Grid.

   WBS tree x budget head, one row per (wbs_id, budget_head_id) cell from
   GET /api/budget/cells, each showing budget / commitment / actual /
   received-not-billed / PR-reservation / available, plus a per-row
   utilisation bar. Drilling a cell loads its budget lines from
   GET /api/budget/lines (project_id + wbs_id + version — the contract does
   not filter lines by budget head, so every line for that WBS element is
   shown, each carrying its own budget_head_id column, rather than silently
   filtering data the caller did not ask this module to filter).

   No static fake data: every row on screen came from a fetch() call. Tests
   intercept the network with Playwright's page.route().
*/

import { listCells, listLines, BudgetApiError } from './budget-api.js';
import { h, text, clear } from '../../core/dom.js';
import { formatINR } from '../../core/format.js';
import { createWbsTreeTable } from '../../components/budget/wbs-tree-table.js';
import { sortHierarchy } from '../../components/budget/wbs-hierarchy.js';

const PAGE_SIZE = 100;

function msgBox(kind, body, { messageId, alert = false, actions = [] } = {}) {
  const icon = { error: '✖', warning: '!', success: '✔', info: '·' }[kind] || '·';
  const attrs = { class: `msg msg-${kind}` };
  if (alert) attrs.role = 'alert';
  return h('div', attrs, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, icon),
    h('div', { class: 'body' }, [
      body,
      actions.length ? h('div', { class: 'actions' }, actions) : null,
      messageId ? h('div', { class: 'mid' }, messageId) : null,
    ].filter(Boolean)),
  ]);
}

function emptyBlock(message) {
  return h('div', { class: 'empty' }, [
    h('div', { class: 'big', 'aria-hidden': 'true' }, '▦'),
    h('div', {}, message),
  ]);
}

export function mountBudgetGrid(root) {
  if (!root) return;
  const liveRegion = document.getElementById('budgetLiveRegion');

  const state = {
    projectId: '',
    wbsId: '',
    budgetHeadId: '',
    rows: [],
    cursor: null,
    hasMore: false,
    loadingMore: false,
    gridState: 'empty', // 'loading' | 'empty' | 'error' | 'permission' | 'ready'
    gridError: null,
  };

  const projectInput = h('input', { id: 'budgetGridProject', type: 'text', autocomplete: 'off' });
  const wbsInput = h('input', { id: 'budgetGridWbs', type: 'text', autocomplete: 'off' });
  const headInput = h('input', { id: 'budgetGridHead', type: 'text', autocomplete: 'off' });

  const applyBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Apply filters');
  const clearBtn = h('button', {
    type: 'button', class: 'btn-sm btn-ghost',
    onClick: () => {
      projectInput.value = ''; wbsInput.value = ''; headInput.value = '';
      applyFilters();
    },
  }, 'Clear filters');

  const filterForm = h('form', {
    class: 'toolbar',
    onSubmit: (ev) => { ev.preventDefault(); applyFilters(); },
  }, [
    h('div', { class: 'field' }, [h('label', { for: 'budgetGridProject' }, 'Project'), projectInput]),
    h('div', { class: 'field' }, [h('label', { for: 'budgetGridWbs' }, 'WBS element (root)'), wbsInput]),
    h('div', { class: 'field' }, [h('label', { for: 'budgetGridHead' }, 'Budget head'), headInput]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), applyBtn]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, ''), clearBtn]),
  ]);

  const statusHost = h('div', { id: 'budgetGridStatusHost' });

  const tree = createWbsTreeTable({
    loadDrillContent: (row) => loadLinesForRow(row),
  });

  const loadMoreBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => loadCells(false) }, 'Load more');
  const paginationInfo = h('span', { class: 'muted small' }, '');
  const pagination = h('div', { class: 'audit-pagination', hidden: true }, [
    paginationInfo, h('span', { class: 'grow' }), loadMoreBtn,
  ]);

  const legend = h('div', { class: 'legend' }, [
    h('span', {}, [h('i', { class: 'k-actual' }), 'Actual']),
    h('span', {}, [h('i', { class: 'k-commit' }), 'Commitment']),
    h('span', {}, [h('i', { class: 'k-avail' }), 'Uncommitted budget']),
  ]);

  root.appendChild(filterForm);
  root.appendChild(statusHost);
  root.appendChild(legend);
  root.appendChild(tree.el);
  root.appendChild(pagination);

  function applyFilters() {
    state.projectId = projectInput.value.trim();
    state.wbsId = wbsInput.value.trim();
    state.budgetHeadId = headInput.value.trim();
    loadCells(true);
  }

  function renderStatus() {
    clear(statusHost);
    statusHost.hidden = state.gridState === 'ready' || state.gridState === 'loading';
    if (state.gridState === 'error') {
      const err = state.gridError;
      statusHost.appendChild(msgBox('error', h('div', {}, err ? err.message
        : 'The budget planning grid could not be loaded.'), {
        messageId: err && err.messageId,
        alert: true,
        actions: [h('button', { type: 'button', class: 'btn-sm', onClick: () => loadCells(true) }, 'Retry')],
      }));
    } else if (state.gridState === 'permission') {
      statusHost.appendChild(emptyBlock(state.gridError ? state.gridError.message
        : 'No budget data was found for this scope.'));
    } else if (state.gridState === 'empty') {
      statusHost.appendChild(emptyBlock(
        'No budget cells match these filters. A cell appears here once a WBS element has an approved '
        + 'budget for a budget head — try widening the project, WBS element or budget head filters above.',
      ));
    }
  }

  async function loadCells(reset) {
    if (reset) {
      state.rows = []; state.cursor = null; state.gridState = 'loading'; state.gridError = null;
      renderStatus();
      tree.el.hidden = false;
      tree.renderSkeleton();
      pagination.hidden = true;
    } else {
      state.loadingMore = true;
      loadMoreBtn.disabled = true;
      loadMoreBtn.textContent = 'Loading…';
    }

    try {
      const data = await listCells({
        projectId: state.projectId || undefined,
        wbsId: state.wbsId || undefined,
        budgetHeadId: state.budgetHeadId || undefined,
        cursor: reset ? undefined : state.cursor || undefined,
        limit: PAGE_SIZE,
      });
      const items = (data && data.items) || [];
      const combined = reset ? items : state.rows.concat(items);
      state.rows = sortHierarchy(combined);
      state.cursor = data ? data.next_cursor : null;
      state.hasMore = !!(data && data.has_more);
      state.gridState = state.rows.length ? 'ready' : 'empty';
      state.loadingMore = false;
      if (liveRegion) {
        liveRegion.textContent = state.gridState === 'ready'
          ? `${state.rows.length} budget cells loaded.`
          : 'No budget cells match these filters.';
      }
    } catch (err) {
      state.loadingMore = false;
      if (err instanceof BudgetApiError && err.kind === 'notfound') {
        state.gridState = 'permission';
        state.gridError = { message: err.message, messageId: err.messageId };
      } else {
        state.gridState = 'error';
        state.gridError = {
          message: err instanceof BudgetApiError ? err.message : 'The budget planning grid could not be loaded. Try again.',
          messageId: err instanceof BudgetApiError ? err.messageId : null,
        };
      }
      if (liveRegion) liveRegion.textContent = 'The budget planning grid could not be loaded.';
    }

    renderStatus();
    if (state.gridState === 'ready') {
      tree.el.hidden = false;
      tree.renderRows(state.rows);
      pagination.hidden = false;
      paginationInfo.textContent = state.rows.length === 1 ? '1 cell loaded' : `${state.rows.length} cells loaded`;
      if (!state.hasMore) paginationInfo.textContent += ' · all matching cells loaded';
      loadMoreBtn.hidden = !state.hasMore;
      loadMoreBtn.disabled = false;
      loadMoreBtn.textContent = 'Load more';
    } else {
      tree.el.hidden = true;
      tree.renderRows([]); // purge any skeleton rows left behind by the loading state
      pagination.hidden = true;
    }
  }

  async function loadLinesForRow(row) {
    const data = await listLines({
      projectId: state.projectId || undefined,
      wbsId: row.wbs_id,
    });
    const items = (data && data.items) || [];
    if (!items.length) {
      return h('div', { class: 'empty' }, [
        h('div', {}, 'No budget lines are recorded for this WBS element yet.'),
      ]);
    }
    const linesTable = h('table', {}, [
      h('caption', { class: 'sr-only' }, `Budget lines for ${row.wbs_path || row.wbs_id}`),
      h('thead', {}, h('tr', {}, [
        h('th', { scope: 'col' }, 'Line'),
        h('th', { scope: 'col' }, 'Budget Head'),
        h('th', { scope: 'col' }, 'Kind'),
        h('th', { scope: 'col', class: 'num' }, 'Amount'),
        h('th', { scope: 'col' }, 'Effective From'),
        h('th', { scope: 'col' }, 'Effective To'),
        h('th', { scope: 'col' }, 'Status'),
        h('th', { scope: 'col' }, 'Justification'),
        h('th', { scope: 'col' }, 'Created'),
        h('th', { scope: 'col', class: 'num' }, 'Version'),
      ])),
      h('tbody', {}, items.map((line) => h('tr', {}, [
        h('th', { scope: 'row', class: 'mono' }, text(line.budget_line_id)),
        h('td', {}, text(line.budget_head_id || '—')),
        h('td', {}, text(line.kind || '—')),
        h('td', { class: 'num' }, text(formatINR(line.amount_paise))),
        h('td', {}, text(line.effective_from || '—')),
        h('td', {}, text(line.effective_to || '—')),
        h('td', {}, text(line.status || '—')),
        h('td', {}, text(line.justification || '—')),
        h('td', {}, text(line.created_by ? `${line.created_by} · ${line.created_at || ''}` : (line.created_at || '—'))),
        h('td', { class: 'num' }, text(line.version_no ?? '—')),
      ]))),
    ]);
    return h('div', { class: 'table-wrap' }, linesTable);
  }

  renderStatus();
  loadCells(true);
}
