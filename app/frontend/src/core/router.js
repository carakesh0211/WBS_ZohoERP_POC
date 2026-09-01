/* app/frontend/src/core/router.js
   The SCR-nn screens, as routes of the SPA shell.

   The problem this closes
   -----------------------
   SCR-28 (Audit Trail Viewer), SCR-09 / SCR-10 / SCR-13 (Budget Planning
   Grid, Version Comparison, Availability Check) and SCR-30 (Settings and
   Master Data) were built as ES modules mounted by standalone host pages —
   audit.html, budget.html, settings.html. They worked, but they were not
   reachable from the application: the shell's hash router in app.js knew
   nothing about them, so nothing in the primary navigation led there and no
   hash deep-linked to them.

   This module is the bridge. It declares each screen once — hash, title,
   breadcrumb, required permission, the host DOM it needs and the mount
   function that fills it — and app.js turns that declaration into a NAV row
   and a `V` view. The standalone host pages keep working unchanged; they are
   simply no longer the only way in.

   Why a separate module, and why dynamic import
   ---------------------------------------------
   app.js is a classic script (the shell must run without a module graph). The
   SCR screens are ES modules. app.js therefore `import()`s this module on
   first navigation to one of these routes, which also means none of the
   feature code is fetched for a user who never opens one of these screens.

   Content-Security-Policy
   -----------------------
   `style-src 'self'` with no 'unsafe-inline': a markup `style="…"` attribute
   is blocked outright. Every element here is built with core/dom.js's h(),
   which refuses a `style` attribute and puts every string through a Text node
   — so nothing in this file can emit a style attribute or an unescaped
   interpolation, structurally rather than by convention.
*/

import { h } from './dom.js';

/**
 * A live region each feature module looks up by id at mount time. It has to
 * exist in the document BEFORE the mount call, which is why every screen's
 * host node carries its own, and why app.js appends the host node before it
 * mounts.
 */
function liveRegion(id) {
  return h('div', { id, class: 'sr-only', role: 'status', 'aria-live': 'polite' });
}

/** A titled panel matching the standalone host pages' section structure. */
function panel(headingId, heading, bodyEl) {
  return h('section', { class: 'scr-panel', 'aria-labelledby': headingId }, [
    h('h2', { id: headingId, class: 'section-title' }, heading),
    bodyEl,
  ]);
}

/**
 * The five screens, in the order they appear in the primary navigation.
 *
 * `hash` deliberately does NOT collide with any id already in app.js's NAV
 * table. `audit`, `budget` and `check` are existing shell views with their own
 * approved baselines; repointing those hashes would silently replace three
 * approved screens with different ones.
 *
 * `build()` returns { node, mount } — the host DOM, and a function called
 * once that node is in the document.
 */
export const SCREENS = [
  {
    id: 'audit-trail',
    scr: 'SCR-28',
    group: 'Governance',
    ico: '⧉',
    label: 'Audit Trail Viewer',
    title: 'Audit Trail Viewer',
    crumbs: ['Home', 'Governance', 'Audit Trail Viewer'],
    need: ['audit.read'],
    build() {
      const root = h('div', { id: 'audit-trail-root' });
      const node = h('div', { class: 'scr-host' }, [root, liveRegion('auditLiveRegion')]);
      return {
        node,
        async mount() {
          const { mountAuditTrail } = await import('../features/audit/audit-trail.js');
          mountAuditTrail(root);
        },
      };
    },
  },
  {
    id: 'budget-grid',
    scr: 'SCR-09',
    group: 'Project Control',
    ico: '▩',
    label: 'Budget Planning Grid (cells)',
    title: 'Budget Planning Grid',
    crumbs: ['Home', 'Budgets', 'Budget Planning Grid'],
    need: ['budget.read'],
    build() {
      const root = h('div');
      const node = h('div', { class: 'scr-host' }, [
        panel('scrBudgetGridTitle', 'Budget Planning Grid', root),
        liveRegion('budgetLiveRegion'),
      ]);
      return {
        node,
        async mount() {
          const { mountBudgetGrid } = await import('../features/budget/budget-grid.js');
          mountBudgetGrid(root);
        },
      };
    },
  },
  {
    id: 'budget-compare',
    scr: 'SCR-10',
    group: 'Project Control',
    ico: '⇎',
    label: 'Budget Version Comparison',
    title: 'Budget Version Comparison',
    crumbs: ['Home', 'Budgets', 'Budget Version Comparison'],
    need: ['budget.read'],
    build() {
      const root = h('div');
      const node = h('div', { class: 'scr-host' }, [
        panel('scrBudgetCompareTitle', 'Budget Version Comparison', root),
        liveRegion('budgetLiveRegion'),
      ]);
      return {
        node,
        async mount() {
          const { mountBudgetCompare } = await import('../features/budget/budget-compare.js');
          mountBudgetCompare(root);
        },
      };
    },
  },
  {
    id: 'budget-availability',
    scr: 'SCR-13',
    group: 'Project Control',
    ico: '⊙',
    label: 'Budget Availability Check (cells)',
    title: 'Budget Availability Check',
    crumbs: ['Home', 'Budgets', 'Budget Availability Check'],
    need: ['budget.check'],
    build() {
      const root = h('div');
      const node = h('div', { class: 'scr-host' }, [
        panel('scrBudgetAvailTitle', 'Budget Availability Check', root),
        liveRegion('budgetLiveRegion'),
      ]);
      return {
        node,
        async mount() {
          const { mountBudgetAvailability } = await import('../features/budget/budget-availability.js');
          mountBudgetAvailability(root);
        },
      };
    },
  },
  {
    id: 'settings',
    scr: 'SCR-30',
    group: 'Governance',
    ico: '⚙',
    label: 'Settings & Master Data',
    title: 'Settings and Master Data',
    crumbs: ['Home', 'Governance', 'Settings and Master Data'],
    need: ['settings.read', 'masters.read'],
    build() {
      // The same ids, roles and classes settings.html uses, so the one
      // controller in features/settings/settings-app.js drives both hosts.
      const hierarchyTab = h('button', {
        type: 'button', id: 'settingsTabHierarchy', class: 'settings-tab', role: 'tab',
        'aria-selected': 'true', 'aria-controls': 'settingsHierarchyPanel',
      }, 'Organisation hierarchy');
      const mastersTab = h('button', {
        type: 'button', id: 'settingsTabMasters', class: 'settings-tab', role: 'tab',
        'aria-selected': 'false', tabindex: '-1', 'aria-controls': 'settingsMastersPanel',
      }, 'Item & vendor masters');

      const node = h('div', { class: 'scr-host' }, [
        h('div', { class: 'settings-tablist', role: 'tablist', 'aria-label': 'Settings sections' },
          [hierarchyTab, mastersTab]),
        h('div', {
          id: 'settingsHierarchyPanel', role: 'tabpanel', 'aria-labelledby': 'settingsTabHierarchy',
        }),
        h('div', {
          id: 'settingsMastersPanel', role: 'tabpanel', 'aria-labelledby': 'settingsTabMasters',
          hidden: true,
        }),
        liveRegion('settingsLiveRegion'),
      ]);
      return {
        node,
        async mount() {
          const { mountSettingsApp } = await import('../features/settings/settings-app.js');
          mountSettingsApp();
        },
      };
    },
  },
];

/** @returns {Object|undefined} the screen declared at this hash. */
export function screenById(id) {
  return SCREENS.find((s) => s.id === id);
}
