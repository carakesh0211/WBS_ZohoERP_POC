/* SCR-11 — Budget Revision Request.
   research/30_contracts/C8_screens.json: "Budget Revision Request".

   READ-ONLY, AND THAT IS STATED ON THE SCREEN RATHER THAN IMPLIED BY THE
   ABSENCE OF A BUTTON.

   Raising, submitting, approving and rejecting a revision all exist and all
   work -- they are POST routes on `/api/budget/revisions*`, owned by
   `app/backend/api/budget.py`, which is not this stream's file. What did NOT
   exist was any way to SEE the revisions: that router offers create, submit,
   approve and reject and no list route at all, so a revision could be raised
   and then never looked at again except through the audit trail.

   This screen is that list. It offers no create control, because a create form
   posting to another stream's route with a body this stream invented is how
   two halves of one feature start disagreeing about what a revision is. A
   disabled button would read as a permission problem, so there is none; the
   note below says what is happening instead.

   `delta_paise` IS SIGNED AND IS RENDERED SIGNED. A supplement and a surrender
   are different events and formatINR puts a negative in parentheses, never in
   colour alone (C6 financial.rule).
*/

import { h } from '../../core/dom.js';
import { listBudgetRevisions } from './closure-api.js';
import { createListScreen } from './closure-list.js';
import { money } from './closure-kit.js';

const COLUMNS = [
  { key: 'revision_id', label: 'Revision' },
  { key: 'capex_code', label: 'Project' },
  { key: 'wbs_code', label: 'WBS' },
  { key: 'budget_head', label: 'Budget head' },
  { key: 'delta_paise', label: 'Delta', numeric: true },
  { key: 'effective_from', label: 'Effective from' },
  { key: 'status', label: 'Status' },
  { key: 'created_by', label: 'Raised by' },
  { key: 'decided_by', label: 'Decided by' },
  { key: 'justification', label: 'Justification', wrap: true },
];

function cell(row, col) {
  if (col.key === 'delta_paise') return money(row.delta_paise);
  const value = row[col.key];
  return (value === null || value === undefined || value === '') ? '—' : String(value);
}

/* Stream C: mounted BEFORE createListScreen()'s own card so it renders above
   the table. This screen's own project/status filter inputs are internal to
   createListScreen and not read here, so the export covers the caller's
   full scope rather than the on-screen filter — the component's own "not
   all filters applied" note says so rather than silently narrowing it. */
function mountExportControl(root) {
  const host = h('div', { id: 'budgetRevisionExportHost', class: 'export-actions-host' });
  root.appendChild(host);
  import('../../components/export-button.js').then(({ mountExportButton }) => {
    mountExportButton({ host, dataset: 'budget_revisions', filtersProvider: () => ({}), label: 'Budget revisions' });
  }).catch(() => { host.textContent = ''; });
}

export function mountBudgetRevisionRequest(root) {
  if (!root) return null;
  mountExportControl(root);
  return createListScreen({
    root,
    id: 'budgetRevision',
    title: 'Budget Revision Request',
    glyph: '↻',
    intro: 'Every budget revision raised against a (WBS element, budget head) control cell, '
      + 'with the signed delta it applies and who decided it. A revision creates spending '
      + 'capacity only once it is APPROVED and effective — a Draft, Submitted or Rejected '
      + 'revision contributes nothing to the approved budget, which is why the status column '
      + 'is next to the amount and not buried at the end.',
    what: 'Budget revision requests',
    emptyMessage: 'No budget revision matches this filter.',
    columns: COLUMNS,
    cell,
    call: (params) => listBudgetRevisions(params),
    filters: [
      { name: 'project_id', label: 'Project id', placeholder: 'e.g. PRJ-DM-01' },
      { name: 'status', label: 'Status', placeholder: 'DRAFT / SUBMITTED / APPROVED' },
    ],
    summary: () => h('div', { class: 'msg msg-info', role: 'note', id: 'budgetRevisionReadOnly' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, 'This screen reads; it does not raise.'),
        h('div', {}, 'Raising, submitting, approving and rejecting a revision are POST routes '
          + 'on /api/budget/revisions and they all work. No control for them is offered here '
          + 'rather than one that posts a body this screen invented. A disabled button would '
          + 'read as a permission problem, which it is not.'),
      ]),
    ]),
  });
}
