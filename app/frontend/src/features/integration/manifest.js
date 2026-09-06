/* app/frontend/src/features/integration/manifest.js
   THE REGISTRATION MANIFEST FOR WAVE 5's TWELVE INTEGRATION SCREENS.

   WHY THIS FILE EXISTS AT ALL
   ---------------------------
   `app/frontend/app.js` and `app/frontend/src/core/router.js` are the LEAD's
   files. This stream owns `app/frontend/src/features/integration/**` and does
   not edit either of them — a previous stream added eight nav entries to
   app.js, overflowed the rail by 194px at 1440 and 336px at 1024, and had them
   withdrawn. So the screens ship as feature modules plus this manifest, and
   the lead performs the two splices below.

   THE TWO SPLICES, VERBATIM
   -------------------------
   1. `app/frontend/src/core/router.js` — add the import at the top and spread
      the array into the existing `SCREENS` export:

          import { INTEGRATION_SCREENS } from '../features/integration/manifest.js';
          …
          export const SCREENS = [
            …the thirteen already there…,
            ...INTEGRATION_SCREENS,
          ];

      Nothing else in router.js changes. `INTEGRATION_SCREENS` entries carry
      exactly the shape router.js's own entries carry — id, scr, group, ico,
      label, title, crumbs, need, build() -> { node, mount } — including the
      per-route stylesheet injection, which is done inside `build().mount()`
      here rather than by a router-level constant, so router.js needs no
      INTEGRATION_STYLES of its own.

   2. `app/frontend/app.js` — app.js is a CLASSIC SCRIPT and cannot import an
      ES module, so its `SCR_ROUTES` table cannot read this file. The rows are
      restated as data in `INTEGRATION_NAV_ROWS` below and must be pasted into
      `SCR_ROUTES` (the array at app.js:205), after the approval block:

          { id: 'integration-setup',          ico: '⊕', label: 'Connection Setup Wizard',        need: ['connector.manage'] },
          { id: 'integration-oauth',          ico: '⚿', label: 'OAuth Authorisation & Consent',  need: ['connector.manage'] },
          { id: 'integration-organisation',   ico: '⌾', label: 'Organisation Selection & Mapping', need: ['connector.manage'] },
          { id: 'integration-scopes',         ico: '⊙', label: 'API Scope & Permission Validation', need: ['connector.read'] },
          { id: 'integration-health',         ico: '◔', label: 'Integration Health & API Usage', need: ['connector.read'] },
          { id: 'integration-outbound-po',    ico: '⇧', label: 'Outbound Purchase Order Queue',  need: ['connector.read'] },
          { id: 'integration-inbound-grn',    ico: '⇩', label: 'Inbound GRN / Purchase Receive Status', need: ['connector.read'] },
          { id: 'integration-inbound-bill',   ico: '⇵', label: 'Inbound Vendor Bill Status',     need: ['connector.read'] },
          { id: 'integration-retry',          ico: '⟲', label: 'Failed Sync & Retry Queue',      need: ['connector.read'] },
          { id: 'integration-events',         ico: '⇄', label: 'Sync History & Control Totals',  need: ['connector.read'] },
          { id: 'integration-reconciliation', ico: '⚖', label: 'Commitment-to-Actual Reconciliation', need: ['budget.read'] },
          { id: 'integration-exceptions',     ico: '⚑', label: 'Reconciliation Exception Queue', need: ['budget.read'] },

      That is the WHOLE of the app.js change. `viewAllowed()` resolves any
      SCR_ROUTES id whether or not it appears in the NAV table, and app.js
      builds one `V` entry per SCR_ROUTES row, so these twelve deep-link, stay
      permission-gated and stay bookmarkable WITHOUT a single nav entry.

      `tests/vrt/integration.spec.js` asserts the two tables agree, by reading
      SCR_ROUTES out of the running page and comparing it to this file — so a
      paste that drops a row or drifts a permission fails the suite.

   NOTHING HERE ASKS FOR A RAIL ENTRY, AND THE MEASUREMENTS SAY WHY
   ---------------------------------------------------------------
   Measured from the running application at the three configured viewports,
   with the rail exactly as approved (22 entries, 840px of content):

       desktop-1440   840px content / 856px rail   →  16px of headroom
       laptop-1024    840px content / 713px rail   → 127px ALREADY OVERFLOWING
       tablet-800     rail is display:none         →  not applicable

   A row measures 30px. Twelve rows is 360px, which takes desktop-1440 from
   16px of headroom to 344px of overflow and laptop-1024 from 127px to 487px.
   One consolidated "Integration" row is 30px and takes desktop-1440 to 870px
   against an 856px rail — the same 14px overflow that was measured for a
   single consolidated "Approvals" row, and for the same reason. There is no
   smaller nav addition available, so the question is REFERRED to the lead with
   the numbers and nothing in this manifest depends on the answer.

   PERMISSIONS
   -----------
   These are PRESENTATIONAL gates. The server decides, always.

     * `connector.manage` (Administrator alone) — the three screens that
       configure a connection.
     * `connector.read` (Administrator, Auditor) — the seven read surfaces,
       including SCR-39, which offers a WRITE (the manual retry) from a
       read-gated screen deliberately: an Auditor must be able to SEE what is
       dead-lettered, and the retry itself is refused by the server for a
       caller without `connector.manage`, and the refusal is rendered as one.
     * `budget.read` (every role) — the two reconciliation surfaces, SCR-18 and
       SCR-27. They read commitment-versus-actual data, not connector state;
       their backing endpoints require only an authenticated principal, and
       gating them on `connector.read` would hide a financial control from
       every finance role in the application.

   SCREEN NUMBERING IS HONEST, AND INCOMPLETE ON PURPOSE
   ----------------------------------------------------
   research/30_contracts/C8_screens.json is frozen at forty screens. Nine of
   these twelve are named in it verbatim and carry their real number. THREE ARE
   NOT IN C8 AT ALL — the outbound purchase-order queue, the inbound GRN /
   Purchase Receive status and the inbound vendor-bill status — and they carry
   `scr: null` rather than an invented SCR-41/42/43. A plausible-looking number
   would manufacture traceability the registry does not provide, and the
   traceability matrix is the thing that is supposed to catch that. REPORTED to
   the lead as a contract gap.
*/

import { h } from '../../core/dom.js';

/**
 * The stylesheets these screens need, injected per route.
 *
 * extensions.css carries `.audit-skel-bar` and `th .sort-btn`, which the shared
 * capex-datatable component needs; integration.css carries only this feature's
 * own layout and lives INSIDE the feature directory because this stream owns
 * nothing at the frontend root. A <link> to either is same-origin and static,
 * so `style-src 'self'` permits both exactly as it permits budget.css,
 * settings.css and approvals.css.
 */
export const INTEGRATION_STYLES = [
  '/static/extensions.css',
  '/static/src/features/integration/integration.css',
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
 * A live region each feature module looks up by id at mount time. It has to
 * exist in the document BEFORE the mount call, which is why every screen's
 * host node carries one, and why app.js appends the host node before mounting.
 *
 * ONE id is shared across all twelve, and that is correct: only one
 * integration screen is mounted at a time and app.js replaces #content
 * wholesale on navigation. Twelve hand-written hosts would be twelve chances
 * to forget the region, and forgetting it fails SILENTLY — `announce()` would
 * do nothing and a screen-reader user would get no feedback on load, empty,
 * error, unavailable or refusal.
 */
function liveRegion(id) {
  return h('div', { id, class: 'sr-only', role: 'status', 'aria-live': 'polite' });
}

/**
 * The build() for an integration screen. All twelve have the same host shape,
 * so the declarations below differ only in the module they load.
 */
function integrationScreen(hostId, moduleFile, exportName) {
  return function build() {
    const root = h('div', { id: `${hostId}-root` });
    const node = h('div', { class: 'scr-host' }, [root, liveRegion('integrationLiveRegion')]);
    return {
      node,
      async mount() {
        await ensureStyles(INTEGRATION_STYLES);
        const mod = await import(`./${moduleFile}`);
        mod[exportName](root);
      },
    };
  };
}

/**
 * The twelve screens, in the order the brief lists them: connection
 * configuration and health, scope validation, the three transaction queues,
 * the retry / dead-letter queue, sync history, and the two reconciliation
 * surfaces.
 *
 * `need` here is the SAME permission list `INTEGRATION_NAV_ROWS` declares.
 * It has to be restated because app.js's gate runs synchronously in render(),
 * long before this module's dynamic import can resolve; the VRT suite asserts
 * the two never drift.
 */
export const INTEGRATION_SCREENS = [
  {
    id: 'integration-setup',
    scr: 'SCR-31',
    group: 'Integration',
    ico: '⊕',
    label: 'Connection Setup Wizard',
    title: 'Zoho ERP Connection Setup Wizard',
    crumbs: ['Home', 'Integration', 'Connection Setup Wizard'],
    need: ['connector.manage'],
    build: integrationScreen('integration-setup', 'connection-setup.js', 'mountConnectionSetup'),
  },
  {
    id: 'integration-oauth',
    scr: 'SCR-32',
    group: 'Integration',
    ico: '⚿',
    label: 'OAuth Authorisation & Consent',
    title: 'Zoho OAuth Authorisation and Consent',
    crumbs: ['Home', 'Integration', 'OAuth Authorisation and Consent'],
    need: ['connector.manage'],
    build: integrationScreen('integration-oauth', 'oauth-consent.js', 'mountOAuthConsent'),
  },
  {
    id: 'integration-organisation',
    scr: 'SCR-33',
    group: 'Integration',
    ico: '⌾',
    label: 'Organisation Selection & Mapping',
    title: 'Zoho Organisation Selection and Mapping',
    crumbs: ['Home', 'Integration', 'Organisation Selection and Mapping'],
    need: ['connector.manage'],
    build: integrationScreen('integration-organisation', 'org-mapping.js', 'mountOrgMapping'),
  },
  {
    id: 'integration-scopes',
    scr: 'SCR-34',
    group: 'Integration',
    ico: '⊙',
    label: 'API Scope & Permission Validation',
    title: 'API Scope and Permission Validation',
    crumbs: ['Home', 'Integration', 'API Scope and Permission Validation'],
    need: ['connector.read'],
    build: integrationScreen('integration-scopes', 'scope-validation.js', 'mountScopeValidation'),
  },
  {
    id: 'integration-health',
    scr: 'SCR-38',
    group: 'Integration',
    ico: '◔',
    label: 'Integration Health & API Usage',
    title: 'Integration Health and API Usage',
    crumbs: ['Home', 'Integration', 'Integration Health and API Usage'],
    need: ['connector.read'],
    build: integrationScreen('integration-health', 'health-dashboard.js', 'mountHealthDashboard'),
  },
  {
    /* NOT IN C8. The frozen forty has no outbound emission queue: SCR-15
       "Purchase Order Commitment View" is the FINANCIAL view of a commitment
       and says nothing about whether the document reached Zoho. Reported as a
       contract gap; `scr` stays null rather than becoming an invented number. */
    id: 'integration-outbound-po',
    scr: null,
    group: 'Integration',
    ico: '⇧',
    label: 'Outbound Purchase Order Queue',
    title: 'Outbound Purchase Order Queue',
    crumbs: ['Home', 'Integration', 'Outbound Purchase Order Queue'],
    need: ['connector.read'],
    build: integrationScreen('integration-outbound-po', 'outbound-po-queue.js', 'mountOutboundPoQueue'),
  },
  {
    /* NOT IN C8. SCR-16 "GRN and Unbilled Receipt View" is the financial view;
       the acquisition status of a Purchase Receive — which is PO-anchored,
       because Zoho ERP has no Purchase Receives list endpoint (OAS-02) — has no
       screen in the frozen forty. Reported as a contract gap. */
    id: 'integration-inbound-grn',
    scr: null,
    group: 'Integration',
    ico: '⇩',
    label: 'Inbound GRN / Purchase Receive Status',
    title: 'Inbound GRN and Purchase Receive Status',
    crumbs: ['Home', 'Integration', 'Inbound GRN and Purchase Receive Status'],
    need: ['connector.read'],
    build: integrationScreen('integration-inbound-grn', 'inbound-grn-status.js', 'mountInboundGrnStatus'),
  },
  {
    /* NOT IN C8, for the same reason: SCR-17 "Vendor Bill and Actual CWIP
       View" is the financial view of a bill, not the acquisition status of
       one. Reported as a contract gap. */
    id: 'integration-inbound-bill',
    scr: null,
    group: 'Integration',
    ico: '⇵',
    label: 'Inbound Vendor Bill Status',
    title: 'Inbound Vendor Bill Status',
    crumbs: ['Home', 'Integration', 'Inbound Vendor Bill Status'],
    need: ['connector.read'],
    build: integrationScreen('integration-inbound-bill', 'inbound-bill-status.js', 'mountInboundBillStatus'),
  },
  {
    id: 'integration-retry',
    scr: 'SCR-39',
    group: 'Integration',
    ico: '⟲',
    label: 'Failed Sync & Retry Queue',
    title: 'Failed Sync and Retry Queue',
    crumbs: ['Home', 'Integration', 'Failed Sync and Retry Queue'],
    need: ['connector.read'],
    build: integrationScreen('integration-retry', 'retry-queue.js', 'mountRetryQueue'),
  },
  {
    id: 'integration-events',
    scr: 'SCR-26',
    group: 'Integration',
    ico: '⇄',
    label: 'Sync History & Control Totals',
    title: 'Integration Event Monitor — Sync History and Control Totals',
    crumbs: ['Home', 'Integration', 'Sync History and Control Totals'],
    need: ['connector.read'],
    build: integrationScreen('integration-events', 'event-monitor.js', 'mountEventMonitor'),
  },
  {
    id: 'integration-reconciliation',
    scr: 'SCR-18',
    group: 'Governance',
    ico: '⚖',
    label: 'Commitment-to-Actual Reconciliation',
    title: 'Commitment-to-Actual Reconciliation',
    crumbs: ['Home', 'Integration', 'Commitment-to-Actual Reconciliation'],
    need: ['budget.read'],
    build: integrationScreen('integration-reconciliation', 'reconciliation-workbench.js', 'mountReconciliationWorkbench'),
  },
  {
    id: 'integration-exceptions',
    scr: 'SCR-27',
    group: 'Governance',
    ico: '⚑',
    label: 'Reconciliation Exception Queue',
    title: 'Reconciliation Exception Queue',
    crumbs: ['Home', 'Integration', 'Reconciliation Exception Queue'],
    need: ['budget.read'],
    build: integrationScreen('integration-exceptions', 'exception-queue.js', 'mountExceptionQueue'),
  },
];

/**
 * The app.js `SCR_ROUTES` rows, derived from the single declaration above so
 * the two tables cannot be edited apart. app.js is a classic script and cannot
 * import this — the rows are pasted, and the VRT suite compares the paste back
 * against this array.
 */
export const INTEGRATION_NAV_ROWS = INTEGRATION_SCREENS.map(({ id, ico, label, need }) => ({
  id, ico, label, need,
}));

/** @returns {Object|undefined} the integration screen declared at this hash. */
export function integrationScreenById(id) {
  return INTEGRATION_SCREENS.find((s) => s.id === id);
}
