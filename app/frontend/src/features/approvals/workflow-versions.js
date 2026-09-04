/* app/frontend/src/features/approvals/workflow-versions.js
   Workflow Version History — every version of one approval definition.

   This screen exists because Contract 1 makes versioning the ONLY way a
   workflow changes. A reviewer asked "why was this approved by only two
   people in March and three in June?" cannot answer from the current
   definition; they need the version that was ACTIVE in March, its effective
   window, and the fact that it was retired rather than edited. That is what
   this lists.

   Nothing here is editable. A retired version is history, and history that
   can be edited is not history.
*/

import { h, text } from '../../core/dom.js';
import { formatAuditTimestamp } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { createStateHost, stateForError } from '../../components/approvals/state-host.js';
import {
  createAnnouncer, createPagination, field, textInput, select, card, queryParam,
} from '../../components/approvals/screen-kit.js';
import { listDefinitionVersions, listDefinitions } from './approvals-api.js';

const PAGE_SIZE = 50;

const TONE = { DRAFT: 'neutral', ACTIVE: 'positive', RETIRED: 'neutral' };
const GLYPH = { DRAFT: '·', ACTIVE: '✔', RETIRED: '⊘' };

function versionStatus(status) {
  const code = String(status || '').toUpperCase();
  return h('span', { class: `status st-${TONE[code] || 'neutral'}` }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, GLYPH[code] || '?'),
    text(code || 'Unknown'),
  ]);
}

export function mountWorkflowVersions(root) {
  if (!root) return;
  const announce = createAnnouncer('approvalsLiveRegion');

  const state = {
    definitionId: queryParam('definition'),
    rows: [], cursor: null, hasMore: false, loading: false,
  };

  const idInput = textInput({ value: state.definitionId });
  const idField = field('workflowVersionsDefinition', 'Definition', idInput);

  // A picker filled from the definitions list, so a reviewer does not have to
  // know an opaque id by heart. It is a convenience over the same API, not a
  // second source of truth.
  const pickSel = select([['', 'Choose a workflow…']], {});
  const pickField = field('workflowVersionsPicker', 'Known workflows', pickSel);
  pickSel.addEventListener('change', () => {
    if (pickSel.value) { idInput.value = pickSel.value; load(pickSel.value, true); }
  });

  const toolbar = h('form', {
    class: 'toolbar approval-toolbar',
    'aria-label': 'Choose a workflow',
    onSubmit: (ev) => { ev.preventDefault(); load(idInput.value.trim(), true); },
  }, [
    pickField.el,
    idField.el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Show history')),
  ]);

  const table = createDataTable({
    caption: 'Every version of this approval workflow, with its status and effective window',
    emptyMessage: 'This workflow has no recorded versions.',
    columns: [
      { key: 'version', label: 'Version', numeric: true, render: (r) => text(String(r.version ?? '—')) },
      { key: 'status', label: 'Status', render: (r) => versionStatus(r.status) },
      { key: 'effective_from', label: 'Effective from', render: (r) => text(formatAuditTimestamp(r.effective_from)) },
      { key: 'effective_to', label: 'Effective to', render: (r) => text(r.effective_to ? formatAuditTimestamp(r.effective_to) : 'open') },
      { key: 'stage_count', label: 'Stages', numeric: true, render: (r) => text(String(r.stage_count ?? 0)) },
      { key: 'rule_count', label: 'Rules', numeric: true, render: (r) => text(String(r.rule_count ?? 0)) },
      {
        key: 'instance_count',
        label: 'Instances routed',
        numeric: true,
        // The number that makes a retired version matter: decisions were taken
        // under it, and those decisions still point at it.
        render: (r) => text(String(r.instance_count ?? 0)),
      },
      { key: 'created_by', label: 'Created by', render: (r) => text(r.created_by ?? '—') },
    ],
  });

  const statusHost = createStateHost({
    id: 'workflowVersionsStatusHost',
    glyph: '⇎',
    emptyMessage: 'No workflow is selected. Choose one above to see how it has changed over time.',
    loadingMessage: 'Loading workflow version history…',
    onRetry: () => load(state.definitionId, true),
  });

  const pagination = createPagination({
    id: 'workflowVersionsPagination',
    onMore: () => load(state.definitionId, false),
  });

  root.appendChild(card('workflowVersionsTitle', 'Workflow Version History', [
    h('p', { class: 'muted small' },
      'A workflow is never edited in place. Each change retires the previous '
      + 'version and creates a new one, so a decision can always be read against '
      + 'the workflow that was actually in force when it was taken.'),
    toolbar, statusHost.el, table.el, pagination.el,
  ]));

  async function fillPicker() {
    try {
      const page = await listDefinitions({ limit: 100 });
      const items = Array.isArray(page && page.items) ? page.items : [];
      const seen = new Set();
      for (const d of items) {
        if (!d.definition_id || seen.has(d.definition_id)) continue;
        seen.add(d.definition_id);
        pickSel.appendChild(h('option', { value: d.definition_id },
          `${d.code ?? d.definition_id} — ${d.object_type ?? ''}`));
      }
      return items;
    } catch {
      // The picker is a convenience. Failing to fill it is not a screen
      // failure: the reference field still works, and the version list below
      // reports its own errors honestly.
      return [];
    }
  }

  async function load(definitionId, reset) {
    if (state.loading) return;
    state.definitionId = definitionId || '';
    idInput.value = state.definitionId;

    if (!state.definitionId) {
      // Clear the skeleton rather than just hiding it: a hidden stale
      // skeleton still matches a query and still reads as "loading".
      table.renderRows([]);
      table.el.hidden = true;
      statusHost.set('empty');
      announce('No workflow is selected.');
      return;
    }

    state.loading = true;
    if (reset) {
      state.rows = []; state.cursor = null; state.hasMore = false;
      statusHost.set('loading');
      table.el.hidden = false;
      table.renderSkeleton();
      announce('Loading workflow version history.');
    }
    pagination.set({ hasMore: state.hasMore, loading: true, shown: state.rows.length });

    try {
      const page = await listDefinitionVersions(state.definitionId, {
        cursor: reset ? undefined : state.cursor || undefined,
        limit: PAGE_SIZE,
      });
      const items = Array.isArray(page && page.items) ? page.items : [];
      state.rows = reset ? items : state.rows.concat(items);
      state.cursor = (page && page.next_cursor) || null;
      state.hasMore = !!(page && page.has_more);

      if (state.rows.length === 0) {
        // Clear the skeleton rather than just hiding it: a hidden stale
        // skeleton still matches a query and still reads as "loading".
        table.renderRows([]);
        table.el.hidden = true;
        statusHost.set('empty', { message: 'This workflow has no recorded versions.' });
        announce('This workflow has no recorded versions.');
      } else {
        statusHost.set('ready');
        table.el.hidden = false;
        table.renderRows(state.rows);
        announce(`${state.rows.length} version${state.rows.length === 1 ? '' : 's'} shown.`);
      }
    } catch (err) {
      // Clear the skeleton rather than just hiding it: a hidden stale
      // skeleton still matches a query and still reads as "loading".
      table.renderRows([]);
      table.el.hidden = true;
      const mapped = stateForError(err, 'The workflow version history could not be loaded. Try again.');
      statusHost.set(mapped.state, mapped);
      announce(mapped.state === 'permission'
        ? 'No workflow versions were found.'
        : 'The workflow version history could not be loaded.');
    } finally {
      state.loading = false;
      pagination.set({ hasMore: state.hasMore, loading: false, shown: state.rows.length });
    }
  }

  (async () => {
    const items = await fillPicker();
    if (!state.definitionId && items.length && items[0].definition_id) {
      pickSel.value = items[0].definition_id;
      await load(items[0].definition_id, true);
      return;
    }
    await load(state.definitionId, true);
  })();
}
