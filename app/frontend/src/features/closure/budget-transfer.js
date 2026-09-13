/* SCR-12 — Budget Transfer.
   research/30_contracts/C8_screens.json: "Budget Transfer Screen".

   BOTH LEGS ARE ALWAYS SHOWN, AND THE SCREEN NEVER SHOWS ONE WITHOUT THE
   OTHER. A transfer is a movement between two control cells; a row rendering
   only the leg the reader happens to be scoped to would look like a budget
   appearing from nowhere or vanishing into it.

   The server enforces the same thing harder: `/api/control/budget-transfers`
   applies the scope predicate to BOTH legs' projects and returns the row only
   if both pass, the same rule `006_rls_coverage.sql` applies at the policy
   level. So a cross-project transfer whose far side is outside the caller's
   scope does not appear here at all -- it is not half-rendered, and it is not
   an error either. That is a deliberate choice recorded in
   `scope_inventory.SCOPED_TABLES`: an OR would disclose the far side.

   READ-ONLY, for the reason SCR-11 gives at length: create / submit / approve
   / reject are POST routes on `/api/budget/transfers*`, owned by
   `app/backend/api/budget.py`, and that router has no list route.

   MSG-BUD-006 is the message a transfer that would leave the source cell
   negative refuses with. It is raised by the SERVER on the write path, and
   this screen does not attempt to predict it: an availability check computed
   in the browser from figures on the page is a second implementation of the
   control, and it would be wrong the moment a commitment lands between the
   read and the write.
*/

import { h } from '../../core/dom.js';
import { listBudgetTransfers } from './closure-api.js';
import { createListScreen } from './closure-list.js';
import { money } from './closure-kit.js';

const COLUMNS = [
  { key: 'transfer_id', label: 'Transfer' },
  { key: 'from', label: 'From cell' },
  { key: 'to', label: 'To cell' },
  { key: 'amount_paise', label: 'Amount', numeric: true },
  { key: 'effective_from', label: 'Effective from' },
  { key: 'status', label: 'Status' },
  { key: 'created_by', label: 'Raised by' },
  { key: 'decided_by', label: 'Decided by' },
  { key: 'justification', label: 'Justification', wrap: true },
];

function leg(wbsCode, headName, projectId) {
  return h('span', {}, [
    h('span', { class: 'mono' }, String(wbsCode || '—')),
    ' · ',
    String(headName || '—'),
    h('div', { class: 'xs muted' }, String(projectId || '—')),
  ]);
}

function cell(row, col) {
  if (col.key === 'amount_paise') return money(row.amount_paise);
  if (col.key === 'from') {
    return leg(row.from_wbs_code, row.from_head, row.from_project_id);
  }
  if (col.key === 'to') {
    return leg(row.to_wbs_code, row.to_head, row.to_project_id);
  }
  const value = row[col.key];
  return (value === null || value === undefined || value === '') ? '—' : String(value);
}

/* Stream C: mounted BEFORE createListScreen()'s own card so it renders above
   the table, not after it. This screen's own project/status inputs are
   internal to createListScreen and not read here — the export therefore
   covers the caller's full scope rather than the on-screen filter, which
   the export component's own "not all filters applied" note makes visible
   rather than silently narrowing or widening what gets exported. */
function mountExportControl(root) {
  const host = h('div', { id: 'budgetTransferExportHost', class: 'export-actions-host' });
  root.appendChild(host);
  import('../../components/export-button.js').then(({ mountExportButton }) => {
    mountExportButton({ host, dataset: 'budget_transfers', filtersProvider: () => ({}), label: 'Budget transfers' });
  }).catch(() => { host.textContent = ''; });
}

export function mountBudgetTransfer(root) {
  if (!root) return null;
  mountExportControl(root);
  return createListScreen({
    root,
    id: 'budgetTransfer',
    title: 'Budget Transfer',
    glyph: '⇄',
    intro: 'Every budget transfer, with both control cells named. A transfer moves approved '
      + 'budget between (WBS element, budget head) cells; it creates no new capacity and the '
      + 'two legs always sum to nothing. A transfer whose far side is outside your scope is '
      + 'not listed at all — half a transfer is worse than none.',
    what: 'Budget transfers',
    emptyMessage: 'No budget transfer matches this filter.',
    columns: COLUMNS,
    cell,
    call: (params) => listBudgetTransfers(params),
    filters: [
      { name: 'project_id', label: 'Project id', placeholder: 'either leg' },
      { name: 'status', label: 'Status', placeholder: 'DRAFT / SUBMITTED / APPROVED' },
    ],
    summary: () => h('div', { class: 'msg msg-info', role: 'note', id: 'budgetTransferReadOnly' }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, '·'),
      h('div', { class: 'body' }, [
        h('strong', {}, 'This screen reads; it does not transfer.'),
        h('div', {}, 'Raising, submitting, approving and rejecting a transfer are POST routes '
          + 'on /api/budget/transfers and they all work. The availability check that refuses a '
          + 'transfer leaving the source cell negative (MSG-BUD-006) runs on the server, at the '
          + 'moment of the write; it is deliberately not predicted here, because a second '
          + 'implementation in the browser would be wrong as soon as a commitment lands between '
          + 'the read and the write.'),
      ]),
    ]),
  });
}
