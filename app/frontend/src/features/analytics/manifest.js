/* app/frontend/src/features/analytics/manifest.js
   THE REGISTRATION MANIFEST FOR WAVE 7's ELEVEN ANALYTICS SCREENS.

   WHY THIS FILE EXISTS AT ALL
   ---------------------------
   `app/frontend/app.js` and `app/frontend/src/core/router.js` are SHARED
   files. This agent owns `app/frontend/src/features/analytics/**` and edits
   neither of them, for the same reason the Wave 5 integration stream did not:
   a previous stream added eight nav entries to app.js, overflowed the rail by
   194px at 1440 and 336px at 1024, and had them withdrawn. So the screens ship
   as feature modules plus this manifest, and the lead performs two splices.

   THE TWO SPLICES, VERBATIM
   -------------------------
   1. `app/frontend/src/core/router.js` — add the import at the top and spread
      the array into the existing `SCREENS` export:

          import { ANALYTICS_SCREENS } from '../features/analytics/manifest.js';
          …
          export const SCREENS = [
            …the entries already there…,
            ...ANALYTICS_SCREENS,
          ];

      Nothing else in router.js changes. `ANALYTICS_SCREENS` entries carry
      exactly the shape router.js's own entries carry — id, scr, group, ico,
      label, title, crumbs, need, build() -> { node, mount } — including the
      per-route stylesheet injection, which is done inside `build().mount()`
      here rather than by a router-level constant, so router.js needs no
      ANALYTICS_STYLES of its own.

   2. `app/frontend/app.js` — app.js is a CLASSIC SCRIPT and cannot import an
      ES module, so its `SCR_ROUTES` table cannot read this file. The rows are
      restated as data in `ANALYTICS_NAV_ROWS` below and must be pasted into
      `SCR_ROUTES`, after the integration block:

      { id: 'analytics-executive',          ico: '▣', label: 'Executive CAPEX Dashboard',   need: ['budget.read'] },
      { id: 'analytics-controller',         ico: '◎', label: 'Project Controller Workbench', need: ['budget.check'] },
      { id: 'analytics-project-list',       ico: '▤', label: 'CAPEX Project List',          need: ['budget.read'] },
      { id: 'analytics-project-object',     ico: '▥', label: 'CAPEX Project Object Page',   need: ['budget.read'] },
      { id: 'analytics-wbs-explorer',       ico: '⌗', label: 'WBS Hierarchy Explorer',      need: ['budget.read'] },
      { id: 'analytics-wbs-tree',           ico: '▦', label: 'WBS Tree Table',              need: ['budget.read'] },
      { id: 'analytics-wbs-element',        ico: '⊡', label: 'WBS Element Detail',          need: ['budget.read'] },
      { id: 'analytics-cwip-ledger',        ico: '₹', label: 'CWIP Ledger',                 need: ['budget.read'] },
      { id: 'analytics-commitment-ageing',  ico: '◷', label: 'Open Commitment Ageing',      need: ['budget.read'] },
      { id: 'analytics-cwip-ageing',        ico: '◔', label: 'CWIP Ageing',                 need: ['budget.read'] },
      { id: 'analytics-exceptions',         ico: '⚠', label: 'Exception & Overrun Monitor', need: ['budget.check'] },

      That is the WHOLE of the app.js change. `viewAllowed()` resolves any
      SCR_ROUTES id whether or not it appears in the NAV table, and app.js
      builds one `V` entry per SCR_ROUTES row, so these eleven deep-link, stay
      permission-gated and stay bookmarkable WITHOUT a single nav entry.

      `tests/vrt/analytics.spec.js` asserts the two tables agree, by reading
      SCR_ROUTES out of the running page and comparing it to this file — so a
      paste that drops a row or drifts a permission fails the suite.

   NOTHING HERE ASKS FOR A RAIL ENTRY
   ----------------------------------
   The rail was measured at 840px of content against an 856px rail at
   desktop-1440 and a 713px rail at laptop-1024 — 16px of headroom on one and
   127px of overflow already on the other. A row is 30px. Eleven rows is 330px.
   There is no smaller nav addition available either: a single consolidated
   "Analytics" row is 30px and takes desktop-1440 to 870px against an 856px
   rail, the same 14px overflow measured for a consolidated "Approvals" row and
   for a consolidated "Integration" row. The question is REFERRED to the lead
   with the numbers, and nothing in this manifest depends on the answer.

   PERMISSIONS — PRESENTATIONAL GATES. THE SERVER DECIDES, ALWAYS.
   ---------------------------------------------------------------
   Nine screens take `budget.read`, which every role in `auth.PERMISSIONS`
   holds. That is deliberate and it is not "gating on nothing": these are
   financial REPORTING surfaces over data the caller's server-side scope
   already narrows, and the thing that keeps one plant's numbers away from
   another plant's controller is the resolved scope, not a permission bit. A
   tighter presentational gate here would hide a control from a finance role
   while changing nothing about what the server would return.

   TWO screens take `budget.check` instead, and both are decisions rather than
   readings:

     * SCR-02, the controller workbench, exists to decide whether a commitment
       can be made — which is exactly what `budget.check` authorises.
     * SCR-25, the exception and overrun monitor, is the screen a controller
       acts on. `budget.check` excludes CapitalisationApprover, who has no
       commitment decision to make and reads capitalisation state on SCR-20/21.

   The difference between the two gates is what `tests/vrt/analytics.spec.js`
   exercises with two identities rather than one: a role that holds
   `budget.read` and not `budget.check` reaches nine of the eleven and is
   redirected from the other two, which is a POSITIVE control on the gate
   rather than a bare negative.

   SCREEN NUMBERING IS HONEST, AND COMPLETE
   ----------------------------------------
   All eleven are named verbatim in research/30_contracts/C8_screens.json and
   all eleven carry their real number. Unlike the Wave 4 approval screens and
   three of the Wave 5 integration screens, this agent invented no SCR number
   and left no `scr: null` — there was no need to.
*/

import { h } from '../../core/dom.js';

/**
 * The stylesheets these screens need, injected per route.
 *
 * extensions.css carries `.audit-skel-bar` and `th .sort-btn`, which the shared
 * capex-datatable component needs; analytics.css carries only this feature's
 * own layout and lives INSIDE the feature directory because this agent owns
 * nothing at the frontend root. A <link> to either is same-origin and static,
 * so `style-src 'self'` permits both exactly as it permits budget.css,
 * settings.css, approvals.css and integration.css.
 *
 * `styles.css` itself is NOT listed and is not touched: it is byte-frozen and
 * SHA-256 pinned, index.html already loads it, and nothing here adds to it.
 */
export const ANALYTICS_STYLES = [
  '/static/extensions.css',
  '/static/src/features/analytics/analytics.css',
];

/* Injected once per href for the life of the page. Deliberately a copy of
   router.js's `ensureStyles` rather than an import of it: router.js does not
   export it, and this agent does not edit router.js to make it do so. */
const loadedStyles = new Set();

function ensureStyles(hrefs) {
  return Promise.all(hrefs.map((href) => {
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

/**
 * A live region each feature module looks up by id at mount time. It must
 * exist in the document BEFORE the mount call, which is why every screen's
 * host node carries one and why app.js appends the host node before mounting.
 *
 * ONE id is shared across all eleven, and that is correct: only one analytics
 * screen is mounted at a time and app.js replaces #content wholesale on
 * navigation. Eleven hand-written hosts would be eleven chances to forget the
 * region, and forgetting it fails SILENTLY — `announce()` would do nothing and
 * a screen-reader user would get no feedback on load, empty, stale, denied,
 * unavailable or refusal.
 */
function liveRegion(id) {
  return h('div', { id, class: 'sr-only', role: 'status', 'aria-live': 'polite' });
}

/**
 * The build() for an analytics screen. All eleven have the same host shape, so
 * the declarations below differ only in the module they load.
 */
function analyticsScreen(hostId, moduleFile, exportName) {
  return function build() {
    const root = h('div', { id: `${hostId}-root` });
    const node = h('div', { class: 'scr-host' }, [root, liveRegion('analyticsLiveRegion')]);
    return {
      node,
      async mount() {
        await ensureStyles(ANALYTICS_STYLES);
        const mod = await import(`./${moduleFile}`);
        mod[exportName](root);
      },
    };
  };
}

/**
 * The eleven screens, grouped as C8 groups them: the portfolio views, the WBS
 * views, and the financial-control views.
 *
 * `need` here is the SAME permission list `ANALYTICS_NAV_ROWS` declares. It
 * has to be restated because app.js's gate runs synchronously in render(),
 * long before this module's dynamic import can resolve; the VRT suite asserts
 * the two never drift.
 *
 * `id` deliberately does not collide with any existing shell view. `home`,
 * `projects` and `wbs` are approved screens with their own pixel baselines;
 * repointing those hashes would silently replace three approved screens with
 * different ones.
 */
export const ANALYTICS_SCREENS = [
  {
    id: 'analytics-executive',
    scr: 'SCR-01',
    group: 'Work',
    ico: '▣',
    label: 'Executive CAPEX Dashboard',
    title: 'Executive CAPEX Dashboard',
    crumbs: ['Home', 'Analytics', 'Executive CAPEX Dashboard'],
    need: ['budget.read'],
    build: analyticsScreen('analytics-executive', 'executive-dashboard.js', 'mountExecutiveDashboard'),
  },
  {
    id: 'analytics-controller',
    scr: 'SCR-02',
    group: 'Project Control',
    ico: '◎',
    label: 'Project Controller Workbench',
    title: 'Project Controller Workbench',
    crumbs: ['Home', 'Analytics', 'Project Controller Workbench'],
    need: ['budget.check'],
    build: analyticsScreen('analytics-controller', 'controller-workbench.js', 'mountControllerWorkbench'),
  },
  {
    id: 'analytics-project-list',
    scr: 'SCR-04',
    group: 'Project Control',
    ico: '▤',
    label: 'CAPEX Project List',
    title: 'CAPEX Project List',
    crumbs: ['Home', 'Analytics', 'CAPEX Project List'],
    need: ['budget.read'],
    build: analyticsScreen('analytics-project-list', 'project-list.js', 'mountProjectList'),
  },
  {
    id: 'analytics-project-object',
    scr: 'SCR-05',
    group: 'Project Control',
    ico: '▥',
    label: 'CAPEX Project Object Page',
    title: 'CAPEX Project Object Page',
    crumbs: ['Home', 'Analytics', 'CAPEX Project Object Page'],
    need: ['budget.read'],
    build: analyticsScreen('analytics-project-object', 'project-object-page.js', 'mountProjectObjectPage'),
  },
  {
    id: 'analytics-wbs-explorer',
    scr: 'SCR-06',
    group: 'Project Control',
    ico: '⌗',
    label: 'WBS Hierarchy Explorer',
    title: 'WBS Hierarchy Explorer',
    crumbs: ['Home', 'Analytics', 'WBS Hierarchy Explorer'],
    need: ['budget.read'],
    build: analyticsScreen('analytics-wbs-explorer', 'wbs-hierarchy-explorer.js', 'mountWbsHierarchyExplorer'),
  },
  {
    id: 'analytics-wbs-tree',
    scr: 'SCR-07',
    group: 'Project Control',
    ico: '▦',
    label: 'WBS Tree Table',
    title: 'WBS Tree Table',
    crumbs: ['Home', 'Analytics', 'WBS Tree Table'],
    need: ['budget.read'],
    build: analyticsScreen('analytics-wbs-tree', 'wbs-tree-table.js', 'mountWbsTreeTable'),
  },
  {
    id: 'analytics-wbs-element',
    scr: 'SCR-08',
    group: 'Project Control',
    ico: '⊡',
    label: 'WBS Element Detail',
    title: 'WBS Element Detail Page',
    crumbs: ['Home', 'Analytics', 'WBS Element Detail Page'],
    need: ['budget.read'],
    build: analyticsScreen('analytics-wbs-element', 'wbs-element-detail.js', 'mountWbsElementDetail'),
  },
  {
    id: 'analytics-cwip-ledger',
    scr: 'SCR-19',
    group: 'Procurement & Actuals',
    ico: '₹',
    label: 'CWIP Ledger',
    title: 'CWIP Ledger',
    crumbs: ['Home', 'Analytics', 'CWIP Ledger'],
    need: ['budget.read'],
    build: analyticsScreen('analytics-cwip-ledger', 'cwip-ledger.js', 'mountCwipLedger'),
  },
  {
    id: 'analytics-commitment-ageing',
    scr: 'SCR-23',
    group: 'Procurement & Actuals',
    ico: '◷',
    label: 'Open Commitment Ageing',
    title: 'Open Commitment Ageing',
    crumbs: ['Home', 'Analytics', 'Open Commitment Ageing'],
    need: ['budget.read'],
    build: analyticsScreen('analytics-commitment-ageing', 'commitment-ageing.js', 'mountCommitmentAgeing'),
  },
  {
    id: 'analytics-cwip-ageing',
    scr: 'SCR-24',
    group: 'Procurement & Actuals',
    ico: '◔',
    label: 'CWIP Ageing',
    title: 'CWIP Ageing',
    crumbs: ['Home', 'Analytics', 'CWIP Ageing'],
    need: ['budget.read'],
    build: analyticsScreen('analytics-cwip-ageing', 'cwip-ageing.js', 'mountCwipAgeing'),
  },
  {
    id: 'analytics-exceptions',
    scr: 'SCR-25',
    group: 'Work',
    ico: '⚠',
    label: 'Exception & Overrun Monitor',
    title: 'Exception and Overrun Monitor',
    crumbs: ['Home', 'Analytics', 'Exception and Overrun Monitor'],
    need: ['budget.check'],
    build: analyticsScreen('analytics-exceptions', 'exception-monitor.js', 'mountExceptionMonitor'),
  },
];

/**
 * The app.js `SCR_ROUTES` rows, DERIVED from the single declaration above so
 * the two tables cannot be edited apart. app.js is a classic script and cannot
 * import this — the rows are pasted, and the VRT suite compares the paste back
 * against this array.
 */
export const ANALYTICS_NAV_ROWS = ANALYTICS_SCREENS.map(({ id, ico, label, need }) => ({
  id, ico, label, need,
}));

/** @returns {Object|undefined} the analytics screen declared at this hash. */
export function analyticsScreenById(id) {
  return ANALYTICS_SCREENS.find((s) => s.id === id);
}

/**
 * The C8 screen ids this feature implements, for the traceability matrix.
 *
 * Exported rather than restated in a document so a screen added or renumbered
 * here cannot leave the matrix behind.
 */
export const ANALYTICS_C8_IDS = ANALYTICS_SCREENS.map((s) => s.scr);
