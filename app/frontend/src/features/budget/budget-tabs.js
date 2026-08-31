/* app/frontend/src/features/budget/budget-tabs.js
   Host-page orchestration for budget.html: a single page carrying all three
   screens this stream owns (SCR-09 Budget Planning Grid, SCR-10 Budget
   Version Comparison, SCR-13 Budget Availability Check), switched with a
   standard WAI-ARIA tabs pattern (roving tabindex, arrow-key navigation)
   rather than three separate host pages, matching this stream's single
   budget.html file ownership.

   All three screens mount immediately (each fetches its own data
   independently, exactly like audit-trail.js does for its one screen), so
   switching tabs is a pure visibility change with no re-fetch.
*/

import { mountBudgetGrid } from './budget-grid.js';
import { mountBudgetCompare } from './budget-compare.js';
import { mountBudgetAvailability } from './budget-availability.js';

const TABS = [
  { id: 'grid', label: 'Planning Grid', panelId: 'budgetPanelGrid', tabId: 'budgetTabGrid', mount: mountBudgetGrid },
  { id: 'compare', label: 'Version Comparison', panelId: 'budgetPanelCompare', tabId: 'budgetTabCompare', mount: mountBudgetCompare },
  { id: 'availability', label: 'Availability Check', panelId: 'budgetPanelAvailability', tabId: 'budgetTabAvailability', mount: mountBudgetAvailability },
];

export function mountBudgetTabs() {
  const tabButtons = TABS.map((t) => document.getElementById(t.tabId)).filter(Boolean);
  const panels = TABS.map((t) => document.getElementById(t.panelId)).filter(Boolean);
  if (!tabButtons.length || !panels.length) return;

  function selectTab(index) {
    TABS.forEach((t, i) => {
      const btn = document.getElementById(t.tabId);
      const panel = document.getElementById(t.panelId);
      if (!btn || !panel) return;
      const selected = i === index;
      btn.setAttribute('aria-selected', String(selected));
      btn.tabIndex = selected ? 0 : -1;
      panel.hidden = !selected;
    });
  }

  tabButtons.forEach((btn, index) => {
    btn.addEventListener('click', () => selectTab(index));
    btn.addEventListener('keydown', (ev) => {
      let target = null;
      if (ev.key === 'ArrowRight') target = (index + 1) % tabButtons.length;
      else if (ev.key === 'ArrowLeft') target = (index - 1 + tabButtons.length) % tabButtons.length;
      else if (ev.key === 'Home') target = 0;
      else if (ev.key === 'End') target = tabButtons.length - 1;
      if (target !== null) {
        ev.preventDefault();
        selectTab(target);
        tabButtons[target].focus();
      }
    });
  });

  selectTab(0);

  for (const t of TABS) {
    const root = document.getElementById(t.panelId);
    if (root) t.mount(root);
  }
}

document.addEventListener('DOMContentLoaded', mountBudgetTabs);
