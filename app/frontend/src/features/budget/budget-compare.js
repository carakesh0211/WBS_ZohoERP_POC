/* app/frontend/src/features/budget/budget-compare.js
   SCR-10 Budget Version Comparison.

   GET /api/budget/versions?project_id= populates the two version pickers;
   GET /api/budget/compare?project_id=&left=&right= renders the side-by-side
   rows {wbs_id, budget_head_id, left_paise, right_paise, delta_paise}, with
   a delta column and a control to collapse (hide) zero-delta rows — every
   value shown is exactly what the API returned; the delta is never
   recomputed client-side from left/right, precisely so a screen can never
   silently disagree with the server on what "changed" means.

   No static fake data: every row on screen came from a fetch() call. Tests
   intercept the network with Playwright's page.route().
*/

import { listVersions, compareVersions, BudgetApiError } from './budget-api.js';
import { h, text, clear, setGeometry } from '../../core/dom.js';
import { formatINR, formatAuditTimestamp } from '../../core/format.js';
import { compareWbsPath } from '../../components/budget/wbs-hierarchy.js';

// NOTE: docs/WAVE2_CONTRACTS.md's compare row shape is
// {"wbs_id","budget_head_id","left_paise","right_paise","delta_paise"} —
// unlike /cells, it carries no wbs_path. Rows are therefore sorted (using
// the same natural, dot-segment-aware comparison the tree grid uses for
// wbs_path) but never indented into a tree here: doing so would mean
// inventing a hierarchy field the frozen contract does not provide.

const SKELETON_ROWS = 6;
const COLUMN_COUNT = 5;

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
    h('div', { class: 'big', 'aria-hidden': 'true' }, '⇌'),
    h('div', {}, message),
  ]);
}

/** Direction glyph so a delta's sign is never carried by colour alone. formatINR
 *  already parenthesises a negative value (C6_tokens.json financial.rule), so the
 *  glyph is an additional, non-colour cue on top of that, not a replacement for it. */
function deltaCell(deltaPaise) {
  const n = Number(deltaPaise) || 0;
  const cell = h('td', { class: 'num' });
  if (n === 0) {
    cell.appendChild(h('span', { class: 'muted' }, [h('span', { 'aria-hidden': 'true' }, '• '), text(formatINR(0))]));
    return cell;
  }
  const up = n > 0;
  if (!up) cell.classList.add('neg');
  cell.appendChild(h('span', {}, [
    h('span', { 'aria-hidden': 'true' }, up ? '▲ ' : '▼ '),
    text(formatINR(deltaPaise)),
  ]));
  return cell;
}

export function mountBudgetCompare(root) {
  if (!root) return;
  const liveRegion = document.getElementById('budgetLiveRegion');

  const state = {
    projectId: '',
    versions: [],
    versionsState: 'empty', // 'loading' | 'empty' | 'error' | 'ready'
    versionsError: null,
    left: '',
    right: '',
    rows: [],
    compareState: 'empty', // 'loading' | 'empty' | 'error' | 'permission' | 'ready'
    compareError: null,
    hideZero: false,
  };

  const projectInput = h('input', { id: 'budgetCompareProject', type: 'text', autocomplete: 'off' });
  const loadVersionsBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Load versions');
  const versionsForm = h('form', {
    class: 'toolbar',
    onSubmit: (ev) => { ev.preventDefault(); loadVersions(); },
  }, [
    h('div', { class: 'field' }, [h('label', { for: 'budgetCompareProject' }, 'Project'), projectInput]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), loadVersionsBtn]),
  ]);

  const versionsStatusHost = h('div', { id: 'budgetCompareVersionsStatus' });

  const leftSelect = h('select', { id: 'budgetCompareLeft', disabled: true }, [h('option', { value: '' }, '—')]);
  const rightSelect = h('select', { id: 'budgetCompareRight', disabled: true }, [h('option', { value: '' }, '—')]);
  const compareBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm', disabled: true }, 'Compare');
  const compareForm = h('form', {
    class: 'toolbar',
    onSubmit: (ev) => { ev.preventDefault(); runCompare(); },
  }, [
    h('div', { class: 'field' }, [h('label', { for: 'budgetCompareLeft' }, 'Left version'), leftSelect]),
    h('div', { class: 'field' }, [h('label', { for: 'budgetCompareRight' }, 'Right version'), rightSelect]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), compareBtn]),
  ]);

  const hideZeroBtn = h('button', {
    type: 'button', class: 'btn-sm',
    'aria-pressed': 'false',
    onClick: () => {
      state.hideZero = !state.hideZero;
      hideZeroBtn.setAttribute('aria-pressed', String(state.hideZero));
      hideZeroBtn.textContent = state.hideZero ? 'Show unchanged rows' : 'Hide unchanged rows';
      renderCompareTable();
    },
  }, 'Hide unchanged rows');
  const zeroCountNote = h('span', { class: 'muted small' }, '');
  const compareToolbar = h('div', { class: 'btn-row' }, [hideZeroBtn, zeroCountNote]);

  const compareStatusHost = h('div', { id: 'budgetCompareStatus' });

  const headRow = h('tr', {}, [
    h('th', { scope: 'col' }, 'WBS'),
    h('th', { scope: 'col' }, 'Budget Head'),
    h('th', { scope: 'col', class: 'num' }, 'Left'),
    h('th', { scope: 'col', class: 'num' }, 'Right'),
    h('th', { scope: 'col', class: 'num' }, 'Delta'),
  ]);
  const thead = h('thead', {}, headRow);
  const tbody = h('tbody');
  const caption = h('caption', { class: 'sr-only' }, 'Budget version comparison by WBS element and budget head');
  const table = h('table', {}, [caption, thead, tbody]);
  const tableWrap = h('div', { class: 'table-wrap', tabindex: '0' }, table);

  root.appendChild(versionsForm);
  root.appendChild(versionsStatusHost);
  root.appendChild(compareForm);
  root.appendChild(compareToolbar);
  root.appendChild(compareStatusHost);
  root.appendChild(tableWrap);

  function renderVersionsStatus() {
    clear(versionsStatusHost);
    versionsStatusHost.hidden = state.versionsState !== 'error';
    if (state.versionsState === 'error') {
      const err = state.versionsError;
      versionsStatusHost.appendChild(msgBox('error', h('div', {}, err ? err.message : 'Versions could not be loaded.'), {
        messageId: err && err.messageId,
        alert: true,
        actions: [h('button', { type: 'button', class: 'btn-sm', onClick: () => loadVersions() }, 'Retry')],
      }));
    }
  }

  async function loadVersions() {
    state.projectId = projectInput.value.trim();
    state.versionsState = 'loading';
    state.versionsError = null;
    leftSelect.disabled = true; rightSelect.disabled = true; compareBtn.disabled = true;
    renderVersionsStatus();

    try {
      const data = await listVersions(state.projectId || undefined);
      state.versions = (data && data.items) || [];
      state.versionsState = state.versions.length ? 'ready' : 'empty';
      clear(leftSelect); clear(rightSelect);
      if (!state.versions.length) {
        leftSelect.appendChild(h('option', { value: '' }, '—'));
        rightSelect.appendChild(h('option', { value: '' }, '—'));
      } else {
        for (const v of state.versions) {
          const opt = () => h('option', { value: String(v.version) },
            `${v.label || v.version} — ${formatAuditTimestamp(v.created_at)} (${formatINR(v.total_paise)})`);
          leftSelect.appendChild(opt());
          rightSelect.appendChild(opt());
        }
        leftSelect.disabled = false; rightSelect.disabled = false; compareBtn.disabled = false;
        // A sensible default: compare the two most recent versions rather than
        // leaving both pickers on the same value.
        leftSelect.value = String(state.versions[0].version);
        if (state.versions.length > 1) rightSelect.value = String(state.versions[1].version);
      }
      if (liveRegion) {
        liveRegion.textContent = state.versions.length
          ? `${state.versions.length} budget versions loaded.`
          : 'No budget versions found for this project.';
      }
    } catch (err) {
      state.versionsState = 'error';
      state.versionsError = {
        message: err instanceof BudgetApiError ? err.message : 'Versions could not be loaded. Try again.',
        messageId: err instanceof BudgetApiError ? err.messageId : null,
      };
      if (liveRegion) liveRegion.textContent = 'Budget versions could not be loaded.';
    }
    renderVersionsStatus();
  }

  function renderCompareStatus() {
    clear(compareStatusHost);
    const hide = state.compareState === 'ready' || state.compareState === 'loading';
    compareStatusHost.hidden = hide;
    if (state.compareState === 'error') {
      const err = state.compareError;
      compareStatusHost.appendChild(msgBox('error', h('div', {}, err ? err.message : 'The comparison could not be loaded.'), {
        messageId: err && err.messageId,
        alert: true,
        actions: [h('button', { type: 'button', class: 'btn-sm', onClick: () => runCompare() }, 'Retry')],
      }));
    } else if (state.compareState === 'permission') {
      compareStatusHost.appendChild(emptyBlock(state.compareError ? state.compareError.message
        : 'No comparison data was found for this scope.'));
    } else if (state.compareState === 'empty') {
      compareStatusHost.appendChild(emptyBlock(
        'No comparison to show yet. Load versions for a project above, choose a left and right '
        + 'version, then Compare — differences between the two versions appear here, by WBS element and budget head.',
      ));
    }
  }

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

  function renderCompareTable() {
    clear(tbody);
    const visibleRows = state.hideZero ? state.rows.filter((r) => Number(r.delta_paise) !== 0) : state.rows;
    const hiddenCount = state.rows.length - state.rows.filter((r) => Number(r.delta_paise) !== 0).length;
    zeroCountNote.textContent = hiddenCount
      ? `${hiddenCount} unchanged ${hiddenCount === 1 ? 'row' : 'rows'} ${state.hideZero ? 'hidden' : 'shown'}`
      : '';
    if (!visibleRows.length) {
      tbody.appendChild(h('tr', {}, h('td', { colspan: String(COLUMN_COUNT) }, [
        h('div', { class: 'empty' }, [
          h('div', { class: 'big', 'aria-hidden': 'true' }, '⇌'),
          h('div', {}, state.hideZero ? 'Every row is unchanged between these two versions.'
            : 'No rows in this comparison.'),
        ]),
      ])));
      return;
    }
    for (const row of visibleRows) {
      const tr = h('tr');
      const wbsCell = h('th', { scope: 'row', class: 'wbs-cell' }, [
        h('span', { class: 'wbs-code' }, row.wbs_id || '—'),
      ]);
      tr.appendChild(wbsCell);
      tr.appendChild(h('td', {}, text(row.budget_head_id || '—')));
      tr.appendChild(h('td', { class: 'num' }, text(formatINR(row.left_paise))));
      tr.appendChild(h('td', { class: 'num' }, text(formatINR(row.right_paise))));
      tr.appendChild(deltaCell(row.delta_paise));
      tbody.appendChild(tr);
    }
  }

  async function runCompare() {
    if (!leftSelect.value || !rightSelect.value) return;
    state.left = leftSelect.value;
    state.right = rightSelect.value;
    state.compareState = 'loading';
    state.compareError = null;
    renderCompareStatus();
    tableWrap.hidden = false;
    renderSkeleton();
    compareToolbar.hidden = true;

    try {
      const data = await compareVersions({ projectId: state.projectId || undefined, left: state.left, right: state.right });
      const rows = (data && data.rows) || [];
      state.rows = rows.slice().sort((a, b) => compareWbsPath(a.wbs_id, b.wbs_id)
        || String(a.budget_head_id || '').localeCompare(String(b.budget_head_id || '')));
      state.compareState = state.rows.length ? 'ready' : 'empty';
      if (liveRegion) {
        liveRegion.textContent = state.compareState === 'ready'
          ? `Comparison loaded: ${state.rows.length} rows.`
          : 'The comparison returned no rows.';
      }
    } catch (err) {
      if (err instanceof BudgetApiError && err.kind === 'notfound') {
        state.compareState = 'permission';
        state.compareError = { message: err.message, messageId: err.messageId };
      } else {
        state.compareState = 'error';
        state.compareError = {
          message: err instanceof BudgetApiError ? err.message : 'The comparison could not be loaded. Try again.',
          messageId: err instanceof BudgetApiError ? err.messageId : null,
        };
      }
      if (liveRegion) liveRegion.textContent = 'The budget comparison could not be loaded.';
    }

    renderCompareStatus();
    if (state.compareState === 'ready') {
      tableWrap.hidden = false;
      compareToolbar.hidden = false;
      renderCompareTable();
    } else {
      tableWrap.hidden = true;
      compareToolbar.hidden = true;
      clear(tbody);
    }
  }

  renderVersionsStatus();
  renderCompareStatus();
  tableWrap.hidden = true;
  compareToolbar.hidden = true;
}
