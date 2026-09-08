/* app/frontend/src/features/mapping/manifest.js
   THE REGISTRATION MANIFEST FOR THE LAST FOUR OF C8's FORTY SCREENS.

       SCR-35  Master Data Mapping Workbench          #mapping-master
       SCR-36  Transaction Field Mapping Workbench    #mapping-fields
       SCR-37  Sync Direction and Scheduling          #mapping-sync
       SCR-40  Connector Audit and Credential Log     #connector-audit

   WHY THIS FILE EXISTS RATHER THAN AN EDIT TO router.js AND app.js
   ----------------------------------------------------------------
   `app/frontend/app.js` and `app/frontend/src/core/router.js` are the LEAD's
   files, and Wave 5's twelve integration screens and Wave 7's analytics
   screens both ship this way for the same reason. There is also a harder
   constraint than ownership, and it is measurable: tests/vrt/spa-routing.spec
   .js asserts

       expect(registry.map((r) => r.id).sort()).toEqual([ …thirteen ids… ]);

   — an EXACT equality on the contents of router.js's SCREENS array. Spreading
   four more entries into it turns that assertion red, and that assertion is a
   deliberate control ("a fourteenth screen appearing without a test still
   fails here"). Editing the control at the same time as adding the thing it
   guards is the move it exists to prevent, so it is not made here. The splice
   is prepared, proven against the running application by
   tests/vrt/mapping.spec.js, and REFERRED to the lead together with the
   integration and analytics manifests that are waiting on the same decision.

   THE TWO SPLICES, VERBATIM
   -------------------------
   1. `app/frontend/src/core/router.js` — add the import and spread the array
      into the existing SCREENS export:

          import { MAPPING_SCREENS } from '../features/mapping/manifest.js';
          …
          export const SCREENS = [
            …the thirteen already there…,
            ...MAPPING_SCREENS,
          ];

      `MAPPING_SCREENS` entries carry exactly the shape router.js's own entries
      carry — id, scr, group, ico, label, title, crumbs, need, build() ->
      { node, mount } — including the per-route stylesheet injection, done
      inside build().mount() here so router.js needs no constant of its own.

   2. `app/frontend/app.js` — app.js is a CLASSIC SCRIPT and cannot import an
      ES module, so its `SCR_ROUTES` table cannot read this file. The rows are
      restated as data in `MAPPING_NAV_ROWS` below and pasted into SCR_ROUTES:

          { id: 'mapping-master',   ico: '⇵', label: 'Master Data Mapping Workbench',       need: ['masters.read'] },
          { id: 'mapping-fields',   ico: '⇄', label: 'Transaction Field Mapping Workbench', need: ['connector.read'] },
          { id: 'mapping-sync',     ico: '◷', label: 'Sync Direction & Scheduling',         need: ['connector.read'] },
          { id: 'connector-audit',  ico: '⧉', label: 'Connector Audit & Credential Log',    need: ['audit.read'] },

      That is the WHOLE of the app.js change. `viewAllowed()` resolves any
      SCR_ROUTES id whether or not it appears in the NAV table, and app.js
      builds one `V` entry per SCR_ROUTES row, so these four deep-link, stay
      permission-gated and stay bookmarkable WITHOUT a single nav entry.

      tests/vrt/mapping.spec.js applies exactly these two splices to the
      running page before the shell's first render, and asserts the paste still
      deep-equals this manifest — so a manifest change nobody carried across
      fails the suite rather than shipping.

   NOTHING HERE ASKS FOR A NAV RAIL ENTRY
   --------------------------------------
   Measured from the running application with the rail exactly as approved,
   the content already overflows laptop-1024 by 127px and clears desktop-1440
   by 16px. A row is 30px; four rows is 120px, which puts desktop-1440 into
   overflow too. The integration manifest referred the same question to the
   lead with the same numbers and nothing here depends on the answer.

   PERMISSIONS — PRESENTATIONAL GATES. THE SERVER DECIDES, ALWAYS.
   ---------------------------------------------------------------
     * `masters.read` (SCR-35) — the permission the /api/masters router itself
       enforces as a dependency on every route. Gating the screen on
       `connector.manage` instead would hide a master-data workbench from the
       finance roles who maintain master data, and would not match what the
       server actually checks.

     * `connector.read` (SCR-36, SCR-37) — held by Administrator and Auditor.
       Both screens read `/api/zoho/inventory*` and `/api/zoho/scopes`, which
       the shell gates on exactly this permission. Neither screen writes;
       the write controls they display are disabled with their reason.

     * `audit.read` (SCR-40) — the permission `/api/audit/*` enforces at its
       router. It is the correct gate for a credential ACTIVITY log and it is
       narrower than `connector.read`: an Auditor holds it, and reading who
       touched a credential is an audit act rather than a connector act.

   SCREEN NUMBERING
   ----------------
   All four are named in research/30_contracts/C8_screens.json verbatim and
   carry their real number. C8 names SCR-37 "Sync Direction and Scheduling
   Configuration"; the label below is shortened for the route table and the
   `title` — which is what the page <h1> renders — carries the verbatim name.
*/

import { h } from '../../core/dom.js';

/**
 * The stylesheets these screens need, injected per route.
 *
 * extensions.css carries `.audit-skel-bar` and `th .sort-btn`, which the
 * shared capex-datatable component needs; mapping.css carries only this
 * feature's own layout and lives INSIDE the feature directory because this
 * stream owns nothing at the frontend root. A <link> to either is same-origin
 * and static, so `style-src 'self'` permits both exactly as it permits
 * budget.css, settings.css, approvals.css and integration.css.
 */
export const MAPPING_STYLES = [
  '/static/extensions.css',
  '/static/src/features/mapping/mapping.css',
];

/* Injected once per href for the life of the page. Deliberately a copy of
   router.js's `ensureStyles` rather than an import of it: router.js does not
   export it, and this stream does not edit router.js to make it do so. */
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
 * host node carries one and why app.js appends the host before mounting.
 *
 * ONE id is shared across all four, and that is correct: only one of them is
 * mounted at a time and app.js replaces #content wholesale on navigation. Four
 * hand-written hosts would be four chances to forget the region, and
 * forgetting it fails SILENTLY — `announce()` would do nothing and a
 * screen-reader user would get no feedback on load, empty, error, unavailable
 * or refusal.
 */
function liveRegion(id) {
  return h('div', { id, class: 'sr-only', role: 'status', 'aria-live': 'polite' });
}

/** The build() for a mapping screen. All four have the same host shape. */
function mappingScreen(hostId, moduleFile, exportName) {
  return function build() {
    const root = h('div', { id: `${hostId}-root` });
    const node = h('div', { class: 'scr-host' }, [root, liveRegion('mappingLiveRegion')]);
    return {
      node,
      async mount() {
        await ensureStyles(MAPPING_STYLES);
        const mod = await import(`./${moduleFile}`);
        mod[exportName](root);
      },
    };
  };
}

/**
 * The four screens.
 *
 * `need` here is the SAME permission list `MAPPING_NAV_ROWS` declares. It has
 * to be restated because app.js's gate runs synchronously in render(), long
 * before this module's dynamic import can resolve; the VRT suite asserts the
 * two never drift.
 */
export const MAPPING_SCREENS = [
  {
    id: 'mapping-master',
    scr: 'SCR-35',
    group: 'Integration',
    ico: '⇵',
    label: 'Master Data Mapping Workbench',
    title: 'Master Data Mapping Workbench',
    crumbs: ['Home', 'Integration', 'Master Data Mapping Workbench'],
    need: ['masters.read'],
    build: mappingScreen('mapping-master', 'master-mapping-workbench.js',
      'mountMasterMappingWorkbench'),
  },
  {
    id: 'mapping-fields',
    scr: 'SCR-36',
    group: 'Integration',
    ico: '⇄',
    label: 'Transaction Field Mapping Workbench',
    title: 'Transaction Field Mapping Workbench',
    crumbs: ['Home', 'Integration', 'Transaction Field Mapping Workbench'],
    need: ['connector.read'],
    build: mappingScreen('mapping-fields', 'field-mapping-workbench.js',
      'mountFieldMappingWorkbench'),
  },
  {
    id: 'mapping-sync',
    scr: 'SCR-37',
    group: 'Integration',
    ico: '◷',
    label: 'Sync Direction & Scheduling',
    /* C8's verbatim name. The rail label above is shortened; this is what the
       page <h1> renders and it is the name the contract froze. */
    title: 'Sync Direction and Scheduling Configuration',
    crumbs: ['Home', 'Integration', 'Sync Direction and Scheduling Configuration'],
    need: ['connector.read'],
    build: mappingScreen('mapping-sync', 'sync-scheduling.js', 'mountSyncScheduling'),
  },
  {
    id: 'connector-audit',
    scr: 'SCR-40',
    group: 'Governance',
    ico: '⧉',
    label: 'Connector Audit & Credential Log',
    title: 'Connector Audit and Credential Activity Log',
    crumbs: ['Home', 'Governance', 'Connector Audit and Credential Activity Log'],
    need: ['audit.read'],
    build: mappingScreen('connector-audit', 'connector-audit-log.js', 'mountConnectorAuditLog'),
  },
];

/**
 * The app.js `SCR_ROUTES` rows, derived from the single declaration above so
 * the two tables cannot be edited apart. app.js is a classic script and cannot
 * import this — the rows are pasted, and the VRT suite compares the paste back
 * against this array.
 */
export const MAPPING_NAV_ROWS = MAPPING_SCREENS.map(({ id, ico, label, need }) => ({
  id, ico, label, need,
}));

/** @returns {Object|undefined} the mapping screen declared at this hash. */
export function mappingScreenById(id) {
  return MAPPING_SCREENS.find((s) => s.id === id);
}
