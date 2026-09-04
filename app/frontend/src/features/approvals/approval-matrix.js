/* app/frontend/src/features/approvals/approval-matrix.js
   Approval Matrix Configuration — the approval definitions, their rules and
   their stages.

   Contract 1: an ACTIVE or RETIRED definition and everything beneath it is
   IMMUTABLE, enforced in the database. Editing a live workflow retroactively
   rewrites decisions already taken under it, so a change is a NEW VERSION —
   the old row is retired, never edited. This screen is built around that fact
   rather than fighting it: an ACTIVE definition offers no edit control at all,
   only "Draft a new version", and the reason is stated on screen rather than
   left for a configurator to discover from a server error.

   The DRAFT -> ACTIVE transition is the only mutation offered here, and it is
   the server's to refuse (DEFINITION_NOT_ACTIVE, DEFINITION_IMMUTABLE).
*/

import { h, text, clear } from '../../core/dom.js';
import { formatAuditTimestamp } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { createStateHost, stateForError, msgBox } from '../../components/approvals/state-host.js';
import {
  createAnnouncer, createPagination, field, select, card, replace,
} from '../../components/approvals/screen-kit.js';
import { listDefinitions, activateDefinition } from './approvals-api.js';

const PAGE_SIZE = 25;

const OBJECT_TYPES = [
  ['', 'All object types'],
  ['purchase_request', 'Purchase request'],
  ['purchase_order', 'Purchase order'],
  ['budget_revision', 'Budget revision'],
  ['vendor_bill', 'Vendor bill'],
  ['capitalisation', 'Capitalisation'],
];

const STATUSES = [
  ['', 'All statuses'],
  ['DRAFT', 'Draft'],
  ['ACTIVE', 'Active'],
  ['RETIRED', 'Retired'],
];

const DEFINITION_TONE = { DRAFT: 'neutral', ACTIVE: 'positive', RETIRED: 'neutral' };
const DEFINITION_GLYPH = { DRAFT: '·', ACTIVE: '✔', RETIRED: '⊘' };

function definitionStatus(status) {
  const code = String(status || '').toUpperCase();
  return h('span', { class: `status st-${DEFINITION_TONE[code] || 'neutral'}` }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, DEFINITION_GLYPH[code] || '?'),
    text(code || 'Unknown'),
  ]);
}

export function mountApprovalMatrix(root) {
  if (!root) return;
  const announce = createAnnouncer('approvalsLiveRegion');

  const state = { rows: [], cursor: null, hasMore: false, loading: false };

  const typeSel = select(OBJECT_TYPES, {});
  const statusSel = select(STATUSES, {});
  const typeField = field('matrixObjectType', 'Object type', typeSel);
  const statusField = field('matrixStatus', 'Status', statusSel);

  const toolbar = h('div', { class: 'toolbar approval-toolbar', role: 'group', 'aria-label': 'Definition filters' }, [
    typeField.el, statusField.el,
    h('div', { class: 'field field-action' },
      h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => load(true) }, 'Apply filters')),
  ]);

  const outcomeHost = h('div', { id: 'approvalMatrixOutcome' });

  const table = createDataTable({
    caption: 'Approval definitions with object type, version, status, rules and stages',
    emptyMessage: 'No approval definitions match these filters.',
    columns: [
      { key: 'code', label: 'Code', render: (r) => h('span', { class: 'mono' }, String(r.code ?? '—')) },
      { key: 'object_type', label: 'Object type', render: (r) => text(r.object_type ?? '—') },
      { key: 'version', label: 'Version', numeric: true, render: (r) => text(String(r.version ?? '—')) },
      { key: 'status', label: 'Status', render: (r) => definitionStatus(r.status) },
      { key: 'entity_id', label: 'Entity', render: (r) => text(r.entity_id || 'All entities') },
      {
        key: 'effective',
        label: 'Effective',
        render: (r) => text(`${r.effective_from ? formatAuditTimestamp(r.effective_from) : '—'} → ${r.effective_to ? formatAuditTimestamp(r.effective_to) : 'open'}`),
      },
      { key: 'rule_count', label: 'Rules', numeric: true, render: (r) => text(String(r.rule_count ?? 0)) },
      { key: 'stage_count', label: 'Stages', numeric: true, render: (r) => text(String(r.stage_count ?? 0)) },
      {
        key: 'actions',
        label: 'Action',
        render: (r) => {
          const status = String(r.status || '').toUpperCase();
          if (status === 'DRAFT') {
            return h('button', {
              type: 'button', class: 'btn-sm', onClick: () => activate(r),
            }, 'Activate');
          }
          // No edit control on an ACTIVE or RETIRED definition. This is not
          // courtesy — the database refuses the write. Offering the control
          // and letting it fail would teach a configurator that the rule is
          // negotiable.
          return h('span', { class: 'approval-cell-note' }, 'Immutable — draft a new version');
        },
      },
    ],
    renderRowDetail: (r) => renderDefinitionDetail(r),
  });

  function renderDefinitionDetail(def) {
    const rules = Array.isArray(def.rules) ? def.rules : [];
    const stages = Array.isArray(def.stages) ? def.stages : [];
    const box = h('div', { class: 'approval-definition-detail' });

    box.appendChild(h('h3', {}, 'Rules, in priority order'));
    box.appendChild(rules.length
      ? h('ol', { class: 'approval-rule-list' }, rules
        .slice()
        .sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0))
        .map((rule) => h('li', {}, [
          h('span', { class: 'mono' }, `priority ${rule.priority ?? '—'}`),
          text(' — '),
          h('code', { class: 'xs' }, JSON.stringify(rule.predicate ?? {})),
        ])))
      : h('p', { class: 'muted' }, 'This definition declares no rules, so nothing routes to it.'));

    box.appendChild(h('h3', {}, 'Stages'));
    box.appendChild(stages.length
      ? h('div', { class: 'table-wrap', tabindex: '0' }, h('table', {}, [
        h('caption', { class: 'sr-only' }, `Stages of definition ${def.code ?? ''}`),
        h('thead', {}, h('tr', {}, [
          h('th', { scope: 'col', class: 'num' }, 'No.'),
          h('th', { scope: 'col' }, 'Name'),
          h('th', { scope: 'col' }, 'Quorum'),
          h('th', { scope: 'col' }, 'Approvers'),
          h('th', { scope: 'col', class: 'num' }, 'SLA (h)'),
          h('th', { scope: 'col', class: 'num' }, 'Escalate after (h)'),
          h('th', { scope: 'col' }, 'Delegation'),
          h('th', { scope: 'col' }, 'Reason'),
        ])),
        h('tbody', {}, stages
          .slice()
          .sort((a, b) => (a.stage_no ?? 0) - (b.stage_no ?? 0))
          .map((s) => h('tr', {}, [
            h('td', { class: 'num' }, text(String(s.stage_no ?? '—'))),
            h('td', {}, text(s.name ?? '—')),
            h('td', {}, text(`${s.quorum_type ?? '—'}${s.quorum_n ? ` (${s.quorum_n})` : ''}`)),
            h('td', {}, (Array.isArray(s.approvers) && s.approvers.length)
              ? h('ul', { class: 'approval-assignees' }, s.approvers.map((a) => h('li', {}, [
                h('span', { class: 'mono' }, `${a.approver_kind ?? '—'}:${a.approver_ref ?? '—'}`),
              ])))
              : text('—')),
            h('td', { class: 'num' }, text(String(s.sla_hours ?? '—'))),
            h('td', { class: 'num' }, text(String(s.escalate_after_hours ?? '—'))),
            h('td', {}, text(s.allow_delegation ? 'Permitted' : 'Not permitted')),
            h('td', {}, text(s.requires_reason
              ? `Required${s.reason_code_set ? ` (${s.reason_code_set})` : ''}`
              : 'Optional')),
          ]))),
      ]))
      : h('p', { class: 'muted' }, 'This definition declares no stages.'));

    return box;
  }

  const statusHost = createStateHost({
    id: 'approvalMatrixStatusHost',
    glyph: '▦',
    emptyMessage: 'No approval definitions match these filters.',
    loadingMessage: 'Loading approval definitions…',
    onRetry: () => load(true),
  });

  const pagination = createPagination({ id: 'approvalMatrixPagination', onMore: () => load(false) });

  root.appendChild(card('approvalMatrixTitle', 'Approval Matrix Configuration', [
    h('p', { class: 'muted small' },
      'An active or retired definition is immutable, and so is every rule, stage '
      + 'and approver beneath it. Changing a live workflow would retroactively '
      + 'rewrite decisions already taken under it, so a change is a new version: '
      + 'the old one is retired, never edited.'),
    toolbar, outcomeHost, statusHost.el, table.el, pagination.el,
  ]));

  async function activate(def) {
    clear(outcomeHost);
    try {
      const result = await activateDefinition(def.definition_id);
      outcomeHost.appendChild(msgBox('success',
        h('div', {}, `${def.code ?? def.definition_id} version ${def.version ?? ''} is now ${(result && result.status) || 'ACTIVE'}. It is immutable from here.`),
        { alert: true }));
      announce('The definition was activated.');
      await load(true);
    } catch (err) {
      outcomeHost.appendChild(msgBox('error', h('div', {}, err.message),
        { messageId: err && err.messageId, alert: true }));
      announce('The definition could not be activated.');
    }
  }

  async function load(reset) {
    if (state.loading) return;
    state.loading = true;
    if (reset) {
      state.rows = []; state.cursor = null; state.hasMore = false;
      statusHost.set('loading');
      table.el.hidden = false;
      table.renderSkeleton();
      announce('Loading approval definitions.');
    }
    pagination.set({ hasMore: state.hasMore, loading: true, shown: state.rows.length });

    try {
      const page = await listDefinitions({
        objectType: typeSel.value || undefined,
        status: statusSel.value || undefined,
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
        statusHost.set('empty');
        announce('No approval definitions match these filters.');
      } else {
        statusHost.set('ready');
        table.el.hidden = false;
        table.renderRows(state.rows);
        announce(`${state.rows.length} approval definition${state.rows.length === 1 ? '' : 's'} shown.`);
      }
    } catch (err) {
      // Clear the skeleton rather than just hiding it: a hidden stale
      // skeleton still matches a query and still reads as "loading".
      table.renderRows([]);
      table.el.hidden = true;
      const mapped = stateForError(err, 'Approval definitions could not be loaded. Try again.');
      statusHost.set(mapped.state, mapped);
      announce(mapped.state === 'permission'
        ? 'No approval definitions were found.'
        : 'Approval definitions could not be loaded.');
    } finally {
      state.loading = false;
      pagination.set({ hasMore: state.hasMore, loading: false, shown: state.rows.length });
    }
  }

  replace(outcomeHost, null);
  load(true);
}
