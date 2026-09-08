/* app/frontend/src/features/analytics/wbs-hierarchy-explorer.js
   SCR-06 — WBS Hierarchy Explorer.

   Named verbatim in research/30_contracts/C8_screens.json as "WBS Hierarchy
   Explorer", so this screen carries its real number.

   FOR FINDING YOUR WAY, NOT FOR READING THE NUMBERS ACROSS
   --------------------------------------------------------
   The explorer opens SHALLOW — roots plus one level, everything below
   collapsed — and carries five money columns rather than ten. That is the
   whole of its difference from SCR-07, and the difference is real even though
   the implementation is shared: a reader looking for an element is navigating,
   and a hierarchy that opens fully expanded at ten columns wide is a wall to
   navigate rather than a map.

   The shared implementation is `wbs-screen-base.js`, and its comment explains
   why the sameness is deliberate rather than accidental.
*/

import { createWbsScreen } from './wbs-screen-base.js';
import { REPORT_IDS } from './analytics-api.js';

export const mountWbsHierarchyExplorer = createWbsScreen({
  screen: 'SCR-06 WBS Hierarchy Explorer',
  idPrefix: 'wbsExp',
  title: 'WBS Hierarchy Explorer',
  intro: 'Opens at the top two levels so the shape of the hierarchy is readable before the '
    + 'numbers are. Each node shows the ROLLUP of everything beneath it — which is why the '
    + '"Holds budget" column is here: adding a parent to its children double counts, every time. '
    + 'Expand a branch to see where the value actually sits.',
  metrics: ['budget', 'commitment', 'actual', 'received_not_billed', 'available'],
  cards: ['budget', 'commitment', 'actual', 'available'],
  collapseBelow: 1,
  reportId: REPORT_IDS.wbsHierarchy,
});
