/* app/frontend/src/features/closure/manifest.js
   THE REGISTRATION MANIFEST FOR THE SIX CLOSURE-STREAM SCREENS.

   WHY THIS FILE WAS MISSING, AND WHAT THAT COST
   ---------------------------------------------
   The six screens below were written, reviewed and committed in Wave 7
   (f1289f8, "commit ten untracked frontend files"). Nothing imported any of
   them. There was no manifest, no entry in `src/core/router.js`'s SCREENS, no
   row in app.js's SCR_ROUTES and no VRT spec — so all six were unreachable in
   the running application, and no test said so because no test referred to
   them at all.

   They are the only implementation of SCR-11, SCR-12, SCR-14, SCR-20, SCR-21
   and SCR-22, which are six of the nine C8 screens the SPA registry did not
   cover. (The other three, SCR-15/16/17, are the legacy shell views `pos`,
   `grns` and `bills` in app.js.) So this is not tidying: without it the
   application does not implement its own screen inventory.

   THE TWO SPLICES, PERFORMED BY THE LEAD IN THE SAME CHANGE
   --------------------------------------------------------
   1. `src/core/router.js` — import CLOSURE_SCREENS and spread it into the
      SCREENS export.
   2. `app.js` — the rows in `CLOSURE_NAV_ROWS` pasted into SCR_ROUTES, because
      app.js is a classic script that cannot import an ES module, and its
      permission gate runs synchronously in render() long before this module's
      dynamic import could resolve.

   NO NAV-RAIL ENTRY, for the reason the integration manifest measured: the
   rail overflows when a stream adds a block of entries to it. `viewAllowed()`
   resolves any SCR_ROUTES id whether or not NAV lists it, so these six
   deep-link, stay bookmarkable and stay permission-gated without one.

   PERMISSIONS ARE NOT INVENTED HERE. Every closure read route sits behind
   `require_closure_access` in `app/backend/api/closure.py`, which requires
   `budget.read` before any route-specific permission is considered. Declaring
   anything narrower here would hide a screen from someone the server would
   have answered; declaring anything wider would show a shell that can only
   ever render a refusal.
*/

import { h } from '../../core/dom.js';

/**
 * The stylesheets these screens need, injected per route.
 *
 * `extensions.css` carries `.audit-skel-bar` and `th .sort-btn` for the shared
 * datatable; `closure.css` carries this feature's own layout. Both are
 * same-origin static files, which `style-src 'self'` permits.
 */
export const CLOSURE_STYLES = [
  '/static/extensions.css',
  '/static/src/features/closure/closure.css',
];

/* Injected once per href for the life of the page. A deliberate copy of
   router.js's `ensureStyles`, matching what the analytics, mapping and
   integration manifests already do — router.js does not export it. */
const loadedStyles = new Set();

function ensureStyles(hrefs) {
  return Promise.all(hrefs.map((href) => {
    if (loadedStyles.has(href)) return Promise.resolve();
    loadedStyles.add(href);
    return new Promise((resolve) => {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = href;
      // Resolve either way: a screen that renders unstyled is still better
      // than a screen that never renders because a stylesheet failed.
      link.addEventListener('load', () => resolve());
      link.addEventListener('error', () => resolve());
      document.head.appendChild(link);
    });
  }));
}

/**
 * The live region every closure screen looks up by id at mount time.
 *
 * `closureLiveRegion` is not a choice made here — `asset-allocation.js`,
 * `capitalisation-workbench.js`, `closure-list.js` and `completion-review.js`
 * already announce into that exact id. It must exist in the document BEFORE
 * mount() runs, which is why the host node carries it and why app.js appends
 * the host before mounting. Getting this wrong fails SILENTLY: `announce()`
 * would write into nothing and a screen-reader user would get no feedback on
 * load, empty, error, unavailable or refusal.
 */
function liveRegion() {
  return h('div', {
    id: 'closureLiveRegion', class: 'sr-only', role: 'status', 'aria-live': 'polite',
  });
}

/** The build() for a closure screen. All six have the same host shape. */
function closureScreen(hostId, moduleFile, exportName) {
  return function build() {
    const root = h('div', { id: hostId + '-root' });
    const node = h('div', { class: 'scr-host' }, [root, liveRegion()]);
    return {
      node,
      async mount() {
        await ensureStyles(CLOSURE_STYLES);
        const mod = await import('./' + moduleFile);
        mod[exportName](root);
      },
    };
  };
}

/**
 * The six screens.
 *
 * `label` is the nav/short form; `title` carries C8's `name_verbatim` so the
 * page heading renders the name the contract froze.
 */
export const CLOSURE_SCREENS = [
  {
    id: 'closure-budget-revision',
    scr: 'SCR-11',
    group: 'Project Control',
    ico: '✎',
    label: 'Budget Revision Request',
    title: 'Budget Revision Request',
    crumbs: ['Home', 'Project Control', 'Budget Revision Request'],
    need: ['budget.read'],
    build: closureScreen('closure-budget-revision', 'budget-revision-request.js',
      'mountBudgetRevisionRequest'),
  },
  {
    id: 'closure-budget-transfer',
    scr: 'SCR-12',
    group: 'Project Control',
    ico: '⇆',
    label: 'Budget Transfer',
    title: 'Budget Transfer Screen',
    crumbs: ['Home', 'Project Control', 'Budget Transfer Screen'],
    need: ['budget.read'],
    build: closureScreen('closure-budget-transfer', 'budget-transfer.js',
      'mountBudgetTransfer'),
  },
  {
    id: 'closure-pr-control',
    scr: 'SCR-14',
    group: 'Work',
    ico: '⊟',
    label: 'Purchase Request Control View',
    title: 'Purchase Request Control View',
    crumbs: ['Home', 'Work', 'Purchase Request Control View'],
    need: ['budget.read'],
    build: closureScreen('closure-pr-control', 'purchase-request-control.js',
      'mountPurchaseRequestControl'),
  },
  {
    id: 'closure-completion-review',
    scr: 'SCR-20',
    group: 'Project Control',
    ico: '◈',
    label: 'Project Completion Review',
    title: 'Project Completion Review',
    crumbs: ['Home', 'Project Control', 'Project Completion Review'],
    need: ['budget.read'],
    build: closureScreen('closure-completion-review', 'completion-review.js',
      'mountCompletionReview'),
  },
  {
    id: 'closure-capitalisation',
    scr: 'SCR-21',
    group: 'Project Control',
    ico: '▦',
    label: 'Capitalisation Workbench',
    title: 'Capitalisation Workbench',
    crumbs: ['Home', 'Project Control', 'Capitalisation Workbench'],
    need: ['budget.read'],
    build: closureScreen('closure-capitalisation', 'capitalisation-workbench.js',
      'mountCapitalisationWorkbench'),
  },
  {
    id: 'closure-asset-allocation',
    scr: 'SCR-22',
    group: 'Project Control',
    ico: '▩',
    label: 'Asset Allocation',
    title: 'Asset Allocation Screen',
    crumbs: ['Home', 'Project Control', 'Asset Allocation Screen'],
    need: ['budget.read'],
    build: closureScreen('closure-asset-allocation', 'asset-allocation.js',
      'mountAssetAllocation'),
  },
];

/**
 * The app.js paste, as data.
 *
 * The same `need` list the SCREENS entries carry, restated because app.js's
 * gate is synchronous. The VRT suite asserts the two never drift.
 */
export const CLOSURE_NAV_ROWS = CLOSURE_SCREENS.map(({ id, ico, label, need }) => ({
  id, ico, label, need,
}));

/** @returns {Object|undefined} the closure screen declared at this id. */
export function closureScreenById(id) {
  return CLOSURE_SCREENS.find((s) => s.id === id);
}

/** The C8 screen numbers this feature implements. */
export const CLOSURE_C8_IDS = CLOSURE_SCREENS.map((s) => s.scr);
