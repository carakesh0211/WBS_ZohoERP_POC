/* app/frontend/src/components/approvals/state-host.js
   The four required states — loading, empty, error, permission-denied —
   rendered once, for eight screens.

   Wave 2's screens each hand-rolled these four states, and the four
   hand-rolled copies drifted: two of them rendered a 403 as an error box, and
   only one of them scoped its error assertion tightly enough to be testable.
   Eight more copies would be eight more chances to drift, so this is the one
   implementation and every approval screen owns only a status host id and its
   own empty-state wording.

   THE PERMISSION-DENIED STATE IS NOT A CLIENT-SIDE AUTHORIZATION DECISION.
   Nothing here inspects the caller's permission set. `permission` is entered
   only in response to a SERVER refusal — and core/api-client.js has already
   applied the not-found-over-forbidden rule by then, so a refused READ arrives
   as kind 'notfound' and a refused WRITE as kind 'forbidden'. The screen
   renders what the server said; it never decides.

   Content-Security-Policy: every node here is built with core/dom.js's h(),
   which throws on a `style` attribute and puts every string through a Text
   node. Nothing in this file can emit a style attribute or an unescaped
   interpolation.
*/

import { h, clear } from '../../core/dom.js';

const GLYPH = { error: '✖', warning: '!', success: '✔', info: '·' };

/**
 * The `.msg` box of app.js's msg(), built as real nodes.
 *
 * @param {'error'|'warning'|'success'|'info'} kind
 * @param {Node|string} body
 * @param {Object} [opts]
 * @param {string|null} [opts.messageId] - rendered as the `.mid` line.
 * @param {boolean} [opts.alert] - role="alert", for something the user must see now.
 * @param {Array<Node>} [opts.actions]
 */
export function msgBox(kind, body, { messageId = null, alert = false, actions = [] } = {}) {
  const attrs = { class: `msg msg-${kind}` };
  if (alert) attrs.role = 'alert';
  return h('div', attrs, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, GLYPH[kind] || GLYPH.info),
    h('div', { class: 'body' }, [
      body,
      actions.length ? h('div', { class: 'actions' }, actions) : null,
      messageId ? h('div', { class: 'mid' }, messageId) : null,
    ].filter(Boolean)),
  ]);
}

/** The `.empty` block: a decorative glyph over one sentence of guidance. */
export function emptyBlock(glyph, message) {
  return h('div', { class: 'empty' }, [
    h('div', { class: 'big', 'aria-hidden': 'true' }, glyph),
    h('div', {}, message),
  ]);
}

/**
 * Create a status host bound to one screen.
 *
 * @param {Object} config
 * @param {string} config.id - a stable, unique element id, so a test can scope
 *   an assertion to THIS screen's status and not to some other hidden
 *   `.msg-error` elsewhere on the page.
 * @param {string} config.glyph - the decorative glyph for the empty state.
 * @param {string} config.emptyMessage
 * @param {string} [config.loadingMessage]
 * @param {Function} [config.onRetry] - when given, the error state offers Retry.
 * @returns {{el: HTMLElement, set: Function, state: Function}}
 */
export function createStateHost({
  id, glyph = '·', emptyMessage = 'There is nothing to show here.',
  loadingMessage = 'Loading…', onRetry = null,
}) {
  const el = h('div', { id });
  let current = 'loading';

  /**
   * @param {'loading'|'empty'|'error'|'permission'|'ready'} next
   * @param {Object} [payload] - { message, messageId } for error/permission,
   *   { message } to override the empty wording.
   */
  function set(next, payload = {}) {
    current = next;
    clear(el);

    if (next === 'ready') {
      el.hidden = true;
      return;
    }
    el.hidden = false;

    if (next === 'loading') {
      el.appendChild(h('div', { class: 'loading' }, payload.message || loadingMessage));
      return;
    }

    if (next === 'empty') {
      el.appendChild(emptyBlock(glyph, payload.message || emptyMessage));
      return;
    }

    if (next === 'permission') {
      // Rendered as an EMPTY state, in the same shape as `empty`, and never
      // with the words "forbidden", "denied" or "403". A 403 on a read that
      // was worded differently from a 404 would be an existence oracle: it
      // would tell the caller that a record they may not see does exist.
      // core/api-client.js has already collapsed the two; this keeps the
      // rendering collapsed too.
      el.appendChild(emptyBlock(glyph, payload.message
        || 'No records were found for these filters.'));
      return;
    }

    // error
    const actions = [];
    if (typeof onRetry === 'function') {
      actions.push(h('button', { type: 'button', class: 'btn-sm', onClick: () => onRetry() }, 'Retry'));
    }
    el.appendChild(msgBox('error',
      h('div', {}, payload.message || 'This could not be loaded. Try again.'),
      { messageId: payload.messageId || null, alert: true, actions }));
  }

  set('loading');
  return { el, set, state: () => current };
}

/**
 * Map an ApiClientError onto one of the four states.
 *
 * The mapping is deliberately narrow: 'notfound' (which core/api-client.js
 * produces for BOTH a genuine 404 and a refused read) becomes `permission`,
 * and everything else becomes `error`. A caller that wants a genuine empty
 * result set must reach `empty` from an EMPTY SUCCESSFUL RESPONSE, never from
 * a failure — an empty list and a refusal are different facts even when they
 * are shown the same way.
 *
 * @param {Error} err
 * @returns {{state: 'permission'|'error', message: string, messageId: string|null}}
 */
export function stateForError(err, fallbackMessage) {
  const kind = err && err.kind;
  const message = (err && err.message) || fallbackMessage || 'This could not be loaded. Try again.';
  const messageId = (err && err.messageId) || null;
  if (kind === 'notfound') return { state: 'permission', message, messageId };
  return { state: 'error', message, messageId };
}
