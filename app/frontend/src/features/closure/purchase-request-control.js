/* SCR-14 — Purchase Request Control View.
   research/30_contracts/C8_screens.json: "Purchase Request Control View".

   A CONTROL VIEW, NOT AN INBOX. It answers one question: which purchase
   requests are consuming budget right now, and on what basis were they let
   through?

   THE FOUR COLUMNS THAT MAKE IT A CONTROL VIEW
   --------------------------------------------
   `check_result`      WITHIN_BUDGET or EXCEEDS_BUDGET — the verdict the
                       availability check returned when the request was raised.
   `exception_reason`  present only where an EXCEEDS_BUDGET request was let
                       through anyway. That is the single most important cell
                       on this screen: a commitment that passed over a refusal
                       is a decision somebody made, and it is shown with its
                       stated reason rather than as a status.
   `reserves_budget`   whether this request takes a hold at all.
   `reserved_paise`    the sum of its LIVE holds only (`state = 'Reserved'`).
                       A Converted or Released hold is history and holds no
                       budget; counting it would overstate what this request is
                       still consuming, and the server's query says so too.

   `reserved_paise` COMES FROM THE SERVER AND IS NOT SUMMED HERE. Money is
   integer paise and no arithmetic on it happens in the browser: a total
   computed here is a total nobody can reconcile against the ledger.
*/

import { h } from '../../core/dom.js';
import { listPurchaseRequests } from './closure-api.js';
import { createListScreen } from './closure-list.js';
import { money } from './closure-kit.js';

const COLUMNS = [
  { key: 'pr_number', label: 'Request' },
  { key: 'capex_code', label: 'Project' },
  { key: 'status', label: 'Status' },
  { key: 'check_result', label: 'Budget verdict' },
  { key: 'amount_paise', label: 'Value', numeric: true },
  { key: 'reserved_paise', label: 'Live hold', numeric: true },
  { key: 'line_count', label: 'Lines', numeric: true },
  { key: 'requested_by', label: 'Raised by' },
  { key: 'approver', label: 'Approved by' },
  { key: 'exception_reason', label: 'Exception reason', wrap: true },
];

const VERDICT_TITLE = {
  WITHIN_BUDGET: 'The availability check passed at the moment this request was raised. '
    + 'It is not a statement about now.',
  EXCEEDS_BUDGET: 'The availability check REFUSED this request. If it is not Rejected, '
    + 'somebody approved it as an exception — the reason is in the last column.',
};

function cell(row, col) {
  if (col.key === 'amount_paise') return money(row.amount_paise);
  if (col.key === 'reserved_paise') {
    // A request that reserves nothing holds nothing. That is a real zero, from
    // the server, not an absent figure — so it renders as a figure.
    return h('span', {
      title: 'Live reservations only. A Converted or Released hold is history and holds no budget.',
    }, money(row.reserved_paise));
  }
  if (col.key === 'check_result') {
    const value = row.check_result;
    if (!value) {
      return h('span', { class: 'muted', title: 'No availability check is recorded against this '
        + 'request. That is not the same as a check that passed.' }, 'not recorded');
    }
    return h('span', { title: VERDICT_TITLE[value] || '' }, String(value));
  }
  if (col.key === 'exception_reason') {
    return row.exception_reason
      ? h('strong', {}, String(row.exception_reason))
      : h('span', { class: 'muted' }, '—');
  }
  const value = row[col.key];
  return (value === null || value === undefined || value === '') ? '—' : String(value);
}

export function mountPurchaseRequestControl(root) {
  return createListScreen({
    root,
    id: 'purchaseRequestControl',
    title: 'Purchase Request Control View',
    glyph: '✎',
    intro: 'Which purchase requests are consuming budget, and on what basis each was let '
      + 'through. The budget verdict is what the availability check returned WHEN THE REQUEST '
      + 'WAS RAISED, not a live re-check; the exception reason is present only where an '
      + 'EXCEEDS_BUDGET request was approved anyway, and it is the most important cell here.',
    what: 'Purchase requests',
    emptyMessage: 'No purchase request matches this filter.',
    columns: COLUMNS,
    cell,
    call: (params) => listPurchaseRequests(params),
    filters: [
      { name: 'project_id', label: 'Project id', placeholder: 'e.g. PRJ-DM-01' },
      { name: 'status', label: 'Status', placeholder: 'Draft / Submitted / Approved' },
    ],
    summary: (rows) => {
      const exceptions = rows.filter((r) => r.exception_reason);
      if (!exceptions.length) return null;
      // A COUNT, not a sum. Counting rows the server sent is not deriving a
      // financial total, and this one earns its place: an exception-approved
      // commitment is the thing a controller opens this screen to find.
      return h('div', { class: 'msg msg-warning', role: 'note', id: 'purchaseRequestExceptions' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
        h('div', { class: 'body' }, [
          h('strong', {}, `${exceptions.length} request${exceptions.length === 1 ? '' : 's'} `
            + 'in this list were approved over a budget refusal.'),
          h('div', {}, 'Each names the reason given. The value they commit is inside the '
            + 'exposure figures everywhere else in this application; it is not held separately.'),
        ]),
      ]);
    },
  });
}
