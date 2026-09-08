/* app/frontend/src/features/integration/integration-screen.js
   The load cycle every integration screen runs, written once.

   THE FIVE STATES, AND WHY THERE ARE FIVE
   ---------------------------------------
   Wave 2's four required states — loading, empty, error, permission-denied —
   are modelled by components/approvals/state-host.js, which this module reuses
   rather than copying. That module's own comment explains why copying is the
   wrong move ("eight more copies would be eight more chances to drift"), and
   the reasoning does not stop at the approval feature boundary: the four
   states behave identically here, including the not-found-over-forbidden rule
   that makes a refused read indistinguishable from a missing one.

   A FIFTH state exists here and nowhere else: UNAVAILABLE — the route this
   screen reads is not mounted in this build. It cannot be folded into any of
   the four:

     * not `empty`: "no records were found" is false when nothing was asked;
     * not `error`: nothing failed, and a Retry button would invite the
       operator to retry something that cannot succeed until another stream
       lands;
     * not `permission`: the operator's permissions are not the reason.

   Streams 1–6 are building those routes right now, so this state is the normal
   case today and the exceptional case in a fortnight. It says which route is
   missing, by name.

   THE data-mounted TRAP
   ---------------------
   app.js sets `data-mounted='1'` only when mount() returns without throwing,
   and sets `data-mount-failed='1'` when it throws. That distinction only works
   if a screen's mount() actually throws on a fatal setup failure and does NOT
   throw on a data-load failure — a screen whose API is down has mounted
   perfectly well and must render its error state, not be reported as unmounted.
   So `run()` below catches every load failure and renders it; only a genuine
   failure to build the screen propagates.
*/

import { h } from '../../core/dom.js';
import { createStateHost, stateForError } from '../../components/approvals/state-host.js';
import { EndpointUnavailableError } from './integration-api.js';
import { redactionWarning, sourceLine, unavailableBlock } from './integration-kit.js';

/**
 * Build the status host plus the source and redaction lines for one screen,
 * and a `run()` that drives them around a single API call.
 *
 * @param {Object} config
 * @param {string} config.id - unique element id, so a test can scope an
 *   assertion to THIS screen's status and not to a hidden one elsewhere.
 * @param {string} config.glyph - decorative glyph for the empty state.
 * @param {string} config.what - the thing this screen reads, in a sentence
 *   fragment: "The dead-letter queue", "Connection profiles".
 * @param {string} config.emptyMessage
 * @param {string} [config.loadingMessage]
 * @param {Function} [config.onRetry]
 * @param {Function} [config.announce]
 * @param {string} [config.idleMessage] - what to show BEFORE any load has been
 *   asked for. See the note below on why the default is "nothing at all".
 */
export function createLoader({
  id, glyph = '·', what, emptyMessage, loadingMessage, onRetry = null, announce = () => {},
  idleMessage = null,
}) {
  const host = createStateHost({
    id, glyph, emptyMessage, loadingMessage: loadingMessage || 'Loading…', onRetry,
  });

  /* A LOADER THAT HAS NOT BEEN ASKED TO LOAD ANYTHING IS NOT LOADING.
     createStateHost() enters `loading` at construction, which is right for a
     host whose screen loads immediately and wrong for one that does not. Two
     of these screens carry a loader that waits for the user (SCR-34's
     validation run) or for another loader to finish (SCR-33's organisation
     discovery, which needs a connection first). Left as constructed, those
     hosts sit showing "Validating every required module…" for a validation
     nobody started — a spinner that never resolves, which is both a lie to the
     reader and, concretely, a `.loading` element that anything waiting for the
     screen to settle waits on forever. It was caught exactly that way: SCR-34
     timed out in settleScreen(). So a loader starts IDLE, and run() is what
     puts it into `loading`. */
  host.set(idleMessage ? 'empty' : 'ready', idleMessage ? { message: idleMessage } : {});
  // Both extra lines live in ONE container so a screen composes a single node
  // and cannot forget one of them.
  const notes = h('div', { class: 'integration-notes' });
  const el = h('div', { class: 'integration-loader' }, [notes, host.el]);

  function clearNotes() {
    while (notes.firstChild) notes.removeChild(notes.firstChild);
  }

  /**
   * Drive one load.
   *
   * @param {Function} call - returns the api module's
   *   `{ data, source, template, redacted }` envelope.
   * @param {Object} handlers
   * @param {(data:*, result:Object)=>boolean} handlers.render - returns TRUE
   *   when it rendered rows, FALSE when the successful response was empty. The
   *   distinction is the caller's because only the caller knows which field
   *   carries the rows.
   * @param {(state:string)=>void} [handlers.onState] - told every state
   *   change, so a screen can hide its table without re-deriving the state.
   * @param {(data:*, result:Object)=>string} [handlers.readyAnnouncement] -
   *   what the live region says once the data is on screen.
   * @returns {Promise<'ready'|'empty'|'error'|'permission'|'unavailable'>}
   */
  async function run(call, { render, onState = () => {}, readyAnnouncement = null }) {
    clearNotes();
    host.set('loading');
    onState('loading');
    try {
      const result = await call();

      // REQ-INT-024. Rendered BEFORE the data, and never suppressed: a server
      // that leaked a secret is a defect the operator must see even though the
      // value itself was discarded before it could reach the DOM.
      const warning = redactionWarning(result.redacted);
      if (warning) notes.appendChild(warning);

      const line = sourceLine(result);
      if (line) notes.appendChild(line);

      const saidBefore = typeof announce.calls === 'number' ? announce.calls : null;
      const hasRows = render(result.data, result);
      if (hasRows) {
        host.set('ready');
        onState('ready');
        /* Every other outcome here reaches the live region; `ready` reached it
           only when the screen's own render happened to announce. The default
           closes that gap WITHOUT talking over a screen that did announce — a
           live region holds one message, so a generic sentence emitted after a
           row count would replace it. See analytics-screen.js for the same
           rule, and analytics-kit.js::createAnnouncer for the counter. */
        const said = readyAnnouncement ? readyAnnouncement(result.data, result) : '';
        const screenSpoke = saidBefore !== null && announce.calls > saidBefore;
        if (said) announce(said);
        else if (!screenSpoke) announce(`${what} is shown.`);
        return 'ready';
      }
      host.set('empty');
      onState('empty');
      announce(emptyMessage);
      return 'empty';
    } catch (err) {
      if (err instanceof EndpointUnavailableError) {
        // Not an error state: nothing failed. The status host renders it as
        // `ready` (hidden) and the note carries the explanation, because the
        // host's four states cannot express "this route does not exist".
        host.set('ready');
        notes.appendChild(unavailableBlock(err, { what }));
        onState('unavailable');
        announce(`${what} is not available in this build.`);
        return 'unavailable';
      }
      const mapped = stateForError(err, `${what} could not be loaded. Try again.`);
      host.set(mapped.state, mapped);
      onState(mapped.state);
      announce(mapped.state === 'permission'
        ? `No records were found for ${what.toLowerCase()}.`
        : `${what} could not be loaded.`);
      return mapped.state;
    }
  }

  return { el, run, host, notes };
}
