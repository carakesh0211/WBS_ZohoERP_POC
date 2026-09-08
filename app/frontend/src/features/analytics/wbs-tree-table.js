/* app/frontend/src/features/analytics/wbs-tree-table.js
   SCR-07 — WBS Tree Table.

   Named verbatim in research/30_contracts/C8_screens.json as "WBS Tree Table",
   so this screen carries its real number.

   FOR READING THE NUMBERS ACROSS A WHOLE HIERARCHY
   ------------------------------------------------
   The tree table opens FLAT — every node expanded — and carries all ten
   metrics, because its reader is not navigating: they are comparing original
   against revised, ordered against committed, received against billed, down a
   column, across every element at once.

   `collapseBelow: null` is what makes it open flat, and it is the only
   structural difference from SCR-06 besides the column set. See
   `wbs-screen-base.js` for why the two screens share an implementation and why
   that is stated rather than hidden.

   THE DOUBLE-COUNTING RULE MATTERS MORE HERE THAN ANYWHERE
   ---------------------------------------------------------
   A fully expanded tree with ten money columns is the single most inviting
   place in this application to select a column and read a total off the bottom
   of it. Every node already contains its descendants, so that total would
   count a five-level project's budget up to five times. The table therefore
   renders NO column sum at all, the "Holds budget" column is present on every
   row, and the project total above is checked against the ROOT rows only —
   which is the sum that is actually true.
*/

import { createWbsScreen } from './wbs-screen-base.js';
import { REPORT_IDS } from './analytics-api.js';

export const mountWbsTreeTable = createWbsScreen({
  screen: 'SCR-07 WBS Tree Table',
  idPrefix: 'wbsTree',
  title: 'WBS Tree Table',
  intro: 'Every element, expanded, with the full decomposition: original, revisions, ordered, '
    + 'open commitment, actual CWIP, received, unbilled receipts, PR reservations and available. '
    + 'There is deliberately no column total: each node already contains its descendants, so a '
    + 'column sum would count a parent’s budget once for every level beneath it. The project '
    + 'total above is the sum of the ROOT elements, and is checked against them.',
  metrics: ['budget', 'original', 'revisions', 'ordered', 'commitment',
    'actual', 'received', 'received_not_billed', 'pr_reserved', 'available'],
  cards: ['budget', 'commitment', 'actual', 'received_not_billed', 'available'],
  collapseBelow: null,
  reportId: REPORT_IDS.wbsHierarchy,
});
