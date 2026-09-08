/* app/frontend/src/features/analytics/analytics-screen.js
   The load cycle every analytics screen runs, written once.

   THE SEVEN STATES
   ----------------
   Wave 2's four — loading, empty, error, permission-denied — are modelled by
   `components/approvals/state-host.js`, which this module REUSES rather than
   copies, for the reason that module's own comment gives: another copy is
   another chance to drift, and the not-found-over-forbidden rule in particular
   must behave identically everywhere.

   Three more exist here, and each is a state the four cannot express:

     UNAVAILABLE   the route is not mounted in this build. Not `empty` ("no
                   rows matched" is false when nothing was asked), not `error`
                   (nothing failed), not `permission` (the caller's grants are
                   not the reason). Established by the integration feature in
                   Wave 5; the same idea, the same rendering discipline.

     DENIED SCOPE  the server answered and DECLARED the caller's resolved scope
                   empty. This is the one the Wave 7 contract is most emphatic
                   about — "an empty table because you hold no grant is not the
                   same as one because nothing matched" — because on an
                   exception monitor the two look identical and mean opposite
                   things. It is entered only on a SERVER DECLARATION, never on
                   a status code this module interpreted for itself: a bare 403
                   on a GET has already been collapsed into `notfound` by
                   core/api-client.js, deliberately, and un-collapsing it would
                   reopen the existence oracle that rule closed.

     STALE         data IS rendered, and is marked as older than it should be.
                   It is a success state with a warning attached rather than a
                   replacement for the data, because hiding a labelled old
                   figure helps nobody and showing it unlabelled is the defect.

   Every one of the seven is reported through `onState` and stamped on the
   loader element as `data-analytics-state`, so a test can prove the screen
   distinguished them rather than inferring it from what the box happens to
   say. That attribute is the assertion surface for
   `tests/vrt/analytics.spec.js`.

   THE data-mounted TRAP
   ---------------------
   app.js sets `data-mounted='1'` only when mount() returns without throwing,
   and `data-mount-failed='1'` when it throws. That distinction works only if a
   screen's mount() throws on a fatal SETUP failure and does NOT throw on a
   DATA-LOAD failure — a screen whose API is down has mounted perfectly well
   and must render its error state. So `run()` catches every load failure and
   renders it; only a genuine failure to build the screen propagates.
*/

import { h } from '../../core/dom.js';
import { createStateHost, stateForError } from '../../components/approvals/state-host.js';
import {
  EndpointUnavailableError, FilterUnavailableError, ScopeDeniedError,
} from './analytics-api.js';
import {
  filterUnavailableBlock, freshnessLine, scopeDeniedBlock, sourceLine, staleBlock,
  unappliedLine, unavailableBlock,
} from './analytics-kit.js';

/** Every state this feature can be in. Exported so the suite cannot drift. */
export const STATES = Object.freeze([
  'loading', 'ready', 'stale', 'empty', 'denied', 'unavailable', 'permission', 'error',
]);

/**
 * Build the status host plus the source, freshness, staleness and
 * unapplied-filter lines for one screen, and a `run()` that drives them around
 * a single API call.
 *
 * @param {Object} config
 * @param {string} config.id - unique element id, so a test can scope an
 *   assertion to THIS screen's status and not to a hidden one elsewhere.
 * @param {string} config.glyph - decorative glyph for the empty state.
 * @param {string} config.what - the thing this screen reads, as a sentence
 *   fragment: "The CWIP ledger", "The exception monitor".
 * @param {string} config.emptyMessage - what "nothing matched" means HERE.
 * @param {string} [config.loadingMessage]
 * @param {Function} [config.onRetry]
 * @param {Function} [config.announce]
 */
export function createLoader({
  id, glyph = '·', what, emptyMessage, loadingMessage, onRetry = null, announce = () => {},
}) {
  const host = createStateHost({
    id, glyph, emptyMessage, loadingMessage: loadingMessage || 'Loading…', onRetry,
  });

  /* A loader that has not been asked to load anything is not loading.
     createStateHost() enters `loading` at construction, which is right for a
     host whose screen loads immediately — every screen in this feature does —
     but run() sets it again explicitly so the state attribute and the host
     never disagree about when the load actually started. */
  const notes = h('div', { class: 'analytics-notes' });
  const el = h('div', { class: 'analytics-loader', 'data-analytics-state': 'loading' }, [
    notes, host.el,
  ]);

  function clearNotes() {
    while (notes.firstChild) notes.removeChild(notes.firstChild);
  }

  function setState(next) {
    el.setAttribute('data-analytics-state', next);
  }

  /**
   * Drive one load.
   *
   * @param {Function} call - returns the api module's
   *   `{ data, source, template, label, freshness, unapplied }` envelope.
   * @param {Object} handlers
   * @param {(data:*, result:Object)=>boolean} handlers.render - returns TRUE
   *   when it rendered rows, FALSE when the successful response was empty.
   *   The distinction is the caller's because only the caller knows which
   *   field carries the rows.
   * @param {(state:string)=>void} [handlers.onState]
   * @returns {Promise<string>} one of STATES.
   */
  async function run(call, { render, onState = () => {} }) {
    clearNotes();
    host.set('loading');
    setState('loading');
    onState('loading');
    try {
      const result = await call();

      // ORDER MATTERS. Provenance is rendered BEFORE the data, every time,
      // because a reader who scrolls to the table and stops must already have
      // passed the line that says where the numbers came from and how old
      // they are.
      const source = sourceLine(result);
      if (source) notes.appendChild(source);
      notes.appendChild(freshnessLine(result.freshness));
      const dropped = unappliedLine(result.unapplied);
      if (dropped) notes.appendChild(dropped);
      const stale = staleBlock(result.freshness);
      if (stale) notes.appendChild(stale);

      const hasRows = render(result.data, result);
      if (!hasRows) {
        host.set('empty');
        setState('empty');
        onState('empty');
        announce(emptyMessage);
        return 'empty';
      }
      host.set('ready');
      const next = result.freshness && result.freshness.stale ? 'stale' : 'ready';
      setState(next);
      onState(next);
      if (next === 'stale') {
        announce(`${what} is shown from stale data that could not be refreshed.`);
      }
      return next;
    } catch (err) {
      if (err instanceof EndpointUnavailableError) {
        // Not an error state: nothing failed. The status host renders as
        // `ready` (hidden) and the note carries the explanation, because the
        // host's four states cannot express "this route does not exist".
        host.set('ready');
        notes.appendChild(unavailableBlock(err, { what }));
        setState('unavailable');
        onState('unavailable');
        announce(`${what} is not available in this build.`);
        return 'unavailable';
      }
      if (err instanceof FilterUnavailableError) {
        /* UNAVAILABLE, not `error` and not `validation`. Nothing failed and
           nothing the reader typed is malformed: the route answered and told
           us which dimension this build cannot express. Rendering it through
           the status host's `error` state would offer a Retry button for a
           request that will be refused identically every time. */
        host.set('ready');
        notes.appendChild(filterUnavailableBlock(err, { what }));
        setState('unavailable');
        onState('unavailable');
        announce(`${what} was not computed, because this build cannot apply one of your filters.`);
        return 'unavailable';
      }
      if (err instanceof ScopeDeniedError) {
        host.set('ready');
        notes.appendChild(scopeDeniedBlock(err, { what }));
        setState('denied');
        onState('denied');
        announce(`${what} is empty because your access scope reaches none of it. `
          + 'This is not a statement that there are no records.');
        return 'denied';
      }
      const mapped = stateForError(err, `${what} could not be loaded. Try again.`);
      host.set(mapped.state, mapped);
      setState(mapped.state);
      onState(mapped.state);
      announce(mapped.state === 'permission'
        ? `No records were found for ${what.toLowerCase()}.`
        : `${what} could not be loaded.`);
      return mapped.state;
    }
  }

  return { el, run, host, notes, state: () => el.getAttribute('data-analytics-state') };
}

/**
 * Mount guard shared by every screen module.
 *
 * A screen's `mount()` must throw only when it cannot BUILD — a missing root
 * node — so that app.js's `data-mount-failed` means what it says. Every screen
 * in this feature calls this first.
 */
export function requireRoot(root, screen) {
  if (!root) {
    throw new Error(`${screen}: no root element was given, so the screen cannot be built.`);
  }
  return root;
}
