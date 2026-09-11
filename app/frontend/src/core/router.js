/* app/frontend/src/core/router.js
   The SCR-nn screens, as routes of the SPA shell.

   The problem this closes
   -----------------------
   SCR-28 (Audit Trail Viewer), SCR-09 / SCR-10 / SCR-13 (Budget Planning
   Grid, Version Comparison, Availability Check) and SCR-30 (Settings and
   Master Data) were built as ES modules mounted by standalone host pages —
   audit.html, budget.html, settings.html. They worked, but they were not
   reachable from the application: the shell's hash router in app.js knew
   nothing about them, so no hash deep-linked to them and nothing inside the
   running application led there at all.

   This module is the bridge. It declares each screen once — hash, title,
   breadcrumb, required permission, the host DOM it needs and the mount
   function that fills it — and app.js turns that declaration into a `V` view.
   The standalone host pages keep working unchanged; they are simply no longer
   the only way in.

   The primary navigation. `group`, `ico` and `label` were carried here unused
   for two waves, against the day the client signed off on listing these
   screens. That happened on 2026-09-03, so app.js's NAV table now reads them:
   the five Wave 2/3 screens are APPROVED UI CHANGE 1 of 2, and the eight
   approval-engine screens added below are listed under the same mechanism,
   each gated on its own Contract 4 permission. See the SCR_ROUTES comment in
   app.js for how the navigation gate and the route gate are kept identical.

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
import { INTEGRATION_SCREENS } from '../features/integration/manifest.js';
import { CLOSURE_SCREENS } from '../features/closure/manifest.js';
import { ANALYTICS_SCREENS } from '../features/analytics/manifest.js';
import { MAPPING_SCREENS } from '../features/mapping/manifest.js';

/* ---------------- on-demand stylesheets ----------------
   index.html loads ONLY the byte-frozen styles.css. The additive stylesheets
   these screens need are injected here, once, the first time one of these
   routes is opened.

   This is not an optimisation, it is a correctness requirement. settings.css
   contains two UNSCOPED selectors --

       input:disabled, textarea:disabled { background: …; color: …; }
       .field label { overflow-wrap: anywhere; }

   -- which, if that stylesheet were linked from index.html, would apply to
   every one of the seventeen client-approved shell views and change how their
   disabled controls and labels render. It does: linking it globally moved the
   `check` (Budget Availability Check) baseline at 800px, where the navigation
   rail is hidden and nothing else could have. Loading per route confines each
   stylesheet to the screens it was written for, so the approved views stay
   byte-identical. The defect itself is REPORTED, not fixed — settings.css is
   not this stream's file.

   A <link> element is same-origin and static, so `style-src 'self'` permits it;
   nothing here builds a style attribute or injects CSS text. */
const loadedStyles = new Set();

function ensureStyles(hrefs) {
  return Promise.all((hrefs || []).map((href) => {
    if (loadedStyles.has(href)) return Promise.resolve();
    loadedStyles.add(href);
    return new Promise((resolve) => {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = href;
      // Resolve either way: a screen that renders unstyled is still better than
      // a screen that never renders because its stylesheet failed to load.
      link.addEventListener('load', () => resolve());
      link.addEventListener('error', () => resolve());
      document.head.appendChild(link);
    });
  }));
}

const AUDIT_STYLES = ['/static/extensions.css'];
const BUDGET_STYLES = ['/static/extensions.css', '/static/budget.css'];
const SETTINGS_STYLES = ['/static/extensions.css', '/static/settings.css'];
/* Budget Setup reuses settings.css's toolbar/dialog/table layout helpers
   (.field-err, .field.grow-action, .settings-pagination, .row-actions — the
   same ones features/settings/masters.js relies on) on top of its own
   budget-setup.css, which carries <governed-select>'s own rules and this
   screen's document-editor layout. budget-categories.js (a masters-CRUD
   screen that happens to live under Governance) needs the identical set for
   the same reason, plus governed-select for its parent-category picker. */
const BUDGET_SETUP_STYLES = ['/static/extensions.css', '/static/budget.css', '/static/settings.css', '/static/src/features/budget/budget-setup.css'];
const BUDGET_CATEGORIES_STYLES = ['/static/extensions.css', '/static/settings.css', '/static/src/features/budget/budget-setup.css'];
const FX_RATES_STYLES = ['/static/extensions.css', '/static/settings.css', '/static/src/features/budget/budget-setup.css', '/static/src/features/settings/settings-fx.css'];
/* extensions.css carries .audit-skel-bar and th .sort-btn, which the shared
   capex-datatable component needs; approvals.css carries only this feature's
   own layout. Unlike settings.css, every selector in approvals.css is scoped
   to an `approval-` class, so it would be harmless even loaded globally — it
   is still loaded per route, because that is the rule here and a stylesheet
   that is safe today is not automatically safe after its next edit. */
const APPROVAL_STYLES = ['/static/extensions.css', '/static/approvals.css'];

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
 * The build() for an approval screen.
 *
 * All eight have the same host shape — one root div plus the live region the
 * feature module looks up by id — so the eight declarations above differ only
 * in the module they load. Writing the host out eight times would be eight
 * chances for one of them to forget its live region, which fails silently:
 * announce() would simply do nothing and a screen-reader user would get no
 * feedback on load, empty, error or refusal.
 *
 * ONE live region id is shared across all eight, and that is correct: only one
 * approval screen is ever mounted at a time, and app.js replaces #content
 * wholesale on navigation.
 */
function approvalScreen(hostId, moduleFile, exportName) {
  return function build() {
    const root = h('div', { id: `${hostId}-root` });
    const node = h('div', { class: 'scr-host' }, [root, liveRegion('approvalsLiveRegion')]);
    return {
      node,
      async mount() {
        await ensureStyles(APPROVAL_STYLES);
        const mod = await import(`../features/approvals/${moduleFile}`);
        mod[exportName](root);
      },
    };
  };
}

/**
 * The thirteen routable screens, in the order they appear in the primary
 * navigation: the five built in Waves 2 and 3, then the approval engine's
 * eight.
 *
 * `need` here is the SAME permission list app.js's SCR_ROUTES declares. It has
 * to be restated because app.js's gate runs synchronously in render(), long
 * before this module's dynamic import can resolve; tests/vrt/spa-routing.spec.js
 * asserts the two never drift.
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
          await ensureStyles(AUDIT_STYLES);
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
          await ensureStyles(BUDGET_STYLES);
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
          await ensureStyles(BUDGET_STYLES);
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
          await ensureStyles(BUDGET_STYLES);
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
          await ensureStyles(SETTINGS_STYLES);
          const { mountSettingsApp } = await import('../features/settings/settings-app.js');
          mountSettingsApp();
        },
      };
    },
  },

  /* ------------------------------------------------------------------
     Wave 4 / M4b — the approval engine's eight screens.

     SCREEN NUMBERING IS HONEST HERE, AND INCOMPLETE ON PURPOSE.
     research/30_contracts/C8_screens.json is frozen at forty screens and
     names only TWO of these eight: SCR-03 "My Approval Inbox" and SCR-29
     "Approval Matrix Configuration". The other six surfaces the M4b brief
     asks for are not in that registry at all. They carry `scr: null` rather
     than an invented number: a plausible-looking SCR-41 would quietly
     manufacture traceability that C8 does not actually provide, and the
     traceability matrix is the thing that is supposed to catch that. This is
     REPORTED to the lead as a contract gap.

     Every one of these mounts a module under features/approvals/, which
     codes against Wave 4 Contract 3's frozen routes. app/backend/api/
     approvals.py is stream 3's file; nothing here imports or assumes
     anything about it beyond those routes.
     ------------------------------------------------------------------ */
  {
    id: 'approval-inbox',
    scr: 'SCR-03',
    group: 'Work',
    ico: '⊞',
    label: 'My Approval Inbox',
    title: 'My Approval Inbox',
    crumbs: ['Home', 'Approvals', 'My Approval Inbox'],
    need: ['approval.read'],
    build: approvalScreen('approval-inbox', 'approval-inbox.js', 'mountApprovalInbox'),
  },
  {
    id: 'approval-request',
    scr: null,
    group: 'Work',
    ico: '▥',
    label: 'Approval Request Detail',
    title: 'Approval Request Detail',
    crumbs: ['Home', 'Approvals', 'Approval Request Detail'],
    need: ['approval.read'],
    build: approvalScreen('approval-request', 'approval-detail.js', 'mountApprovalDetail'),
  },
  {
    id: 'approval-sla',
    scr: null,
    group: 'Work',
    ico: '◷',
    label: 'Escalation & SLA Monitor',
    title: 'Escalation and SLA Monitor',
    crumbs: ['Home', 'Approvals', 'Escalation and SLA Monitor'],
    need: ['approval.read'],
    build: approvalScreen('approval-sla', 'sla-monitor.js', 'mountSlaMonitor'),
  },
  {
    id: 'approval-timeline',
    scr: null,
    group: 'Governance',
    ico: '⧗',
    label: 'Approval Timeline',
    title: 'Approval Timeline and Audit History',
    crumbs: ['Home', 'Governance', 'Approval Timeline and Audit History'],
    need: ['approval.read'],
    build: approvalScreen('approval-timeline', 'approval-timeline.js', 'mountApprovalTimeline'),
  },
  {
    id: 'approval-matrix',
    scr: 'SCR-29',
    group: 'Governance',
    ico: '▨',
    label: 'Approval Matrix Configuration',
    title: 'Approval Matrix Configuration',
    crumbs: ['Home', 'Governance', 'Approval Matrix Configuration'],
    need: ['approval.configure'],
    build: approvalScreen('approval-matrix', 'approval-matrix.js', 'mountApprovalMatrix'),
  },
  {
    id: 'approval-versions',
    scr: null,
    group: 'Governance',
    ico: '⎘',
    label: 'Workflow Version History',
    title: 'Workflow Version History',
    crumbs: ['Home', 'Governance', 'Workflow Version History'],
    need: ['approval.configure'],
    build: approvalScreen('approval-versions', 'workflow-versions.js', 'mountWorkflowVersions'),
  },
  {
    id: 'approval-simulator',
    scr: null,
    group: 'Governance',
    ico: '⊛',
    label: 'Approval Rule Simulator',
    title: 'Approval Rule Simulator',
    crumbs: ['Home', 'Governance', 'Approval Rule Simulator'],
    need: ['approval.configure'],
    build: approvalScreen('approval-simulator', 'rule-simulator.js', 'mountRuleSimulator'),
  },
  {
    id: 'approval-delegations',
    scr: null,
    group: 'Governance',
    ico: '⇌',
    label: 'Delegation Management',
    title: 'Delegation Management',
    crumbs: ['Home', 'Governance', 'Delegation Management'],
    need: ['approval.delegate'],
    build: approvalScreen('approval-delegations', 'delegations.js', 'mountDelegations'),
  },
  // Wave 7. Spliced by the lead, exactly as each feature's manifest.js
  // specifies. The entries carry the same shape router.js's own do -- id, scr,
  // group, ico, label, title, crumbs, need, build() -> { node, mount } -- with
  // per-route stylesheet injection done inside build().mount(), so router.js
  // needs no constant of its own for either.
  // Wave 5's twelve, spliced late: the manifest asked for this in Wave 5
  // and it was not done, so the screens shipped unreachable. See the
  // commit message.
  ...INTEGRATION_SCREENS,
  ...ANALYTICS_SCREENS,
  ...MAPPING_SCREENS,
  // The six closure screens, which had no manifest at all until now and
  // were therefore the only implementation of SCR-11/12/14/20/21/22
  // sitting entirely outside the application.
  ...CLOSURE_SCREENS,

  /* ------------------------------------------------------------------
     Fable 5.1: "Budget Setup" and "Budget Categories" -- the frontend for
     app/backend/api/budgets_original.py (migration 026), which shipped with
     no screen naming it at all. Neither carries an SCR number: C8_screens.
     json is frozen at forty and names nothing for original-budget creation
     or its category master, the same honesty the eight approval-engine
     screens above already use rather than inventing a plausible-looking
     number this frozen registry never allocated.
     ------------------------------------------------------------------ */
  {
    id: 'budget-setup',
    scr: null,
    group: 'Project Control',
    ico: '✚',
    label: 'Budget Setup',
    title: 'Budget Setup',
    crumbs: ['Home', 'Budgets', 'Budget Setup'],
    need: ['budget.read'],
    build() {
      const root = h('div');
      const node = h('div', { class: 'scr-host' }, [
        panel('scrBudgetSetupTitle', 'Budget Setup', root),
        liveRegion('budgetLiveRegion'),
      ]);
      return {
        node,
        async mount() {
          await ensureStyles(BUDGET_SETUP_STYLES);
          const { mountBudgetSetup } = await import('../features/budget/budget-setup.js');
          mountBudgetSetup(root);
        },
      };
    },
  },
  {
    id: 'budget-categories',
    scr: null,
    group: 'Governance',
    ico: '⌸',
    label: 'Budget Categories',
    title: 'Budget Categories',
    crumbs: ['Home', 'Governance', 'Budget Categories'],
    need: ['settings.read'],
    build() {
      const root = h('div');
      const node = h('div', { class: 'scr-host' }, [
        panel('scrBudgetCategoriesTitle', 'Budget Categories', root),
        liveRegion('budgetCategoriesLiveRegion'),
      ]);
      return {
        node,
        async mount() {
          await ensureStyles(BUDGET_CATEGORIES_STYLES);
          const { mountBudgetCategories } = await import('../features/settings/budget-categories.js');
          mountBudgetCategories(root);
        },
      };
    },
  },
  /* Fable 5.1: "Exchange Rates" -- the frontend for app/backend/api/fx_admin.py
     (migration 028), the rate book every foreign-currency translation cites.
     No SCR number, for the reason the two rows above give. */
  {
    id: 'fx-rates',
    scr: null,
    group: 'Governance',
    ico: '⇄',
    label: 'Exchange Rates',
    title: 'Exchange Rates',
    crumbs: ['Home', 'Governance', 'Exchange Rates'],
    need: ['fx.read'],
    build() {
      const root = h('div');
      const node = h('div', { class: 'scr-host' }, [
        panel('scrFxRatesTitle', 'Exchange Rates', root),
        liveRegion('fxRatesLiveRegion'),
      ]);
      return {
        node,
        async mount() {
          await ensureStyles(FX_RATES_STYLES);
          const { mountFxRates } = await import('../features/settings/fx-rates.js');
          mountFxRates(root);
        },
      };
    },
  },
];

/** @returns {Object|undefined} the screen declared at this hash. */
export function screenById(id) {
  return SCREENS.find((s) => s.id === id);
}
