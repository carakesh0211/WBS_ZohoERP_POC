/* app/frontend/src/components/approvals/screen-kit.js
   The parts every approval screen needs and none of them should re-invent:
   a live-region announcer, a labelled toolbar field, a cursor pagination
   control, and a section wrapper.

   Nothing here decides anything. It is layout and accessibility plumbing, so
   that the eight screen modules contain only what is actually different
   between them.
*/

import { h, clear } from '../../core/dom.js';

/**
 * An announcer bound to a live region that the HOST created.
 *
 * The region is looked up by id and never created here. src/core/router.js
 * appends each screen's host node — which carries its own
 * `<div class="sr-only" role="status" aria-live="polite">` — to the document
 * BEFORE it awaits mount(), precisely so this lookup succeeds. A feature that
 * created its own region would produce two live regions on a page where the
 * standalone host already has one, and a screen reader would read the same
 * message twice.
 */
export function createAnnouncer(regionId) {
  const region = document.getElementById(regionId);
  return function announce(message) {
    if (region) region.textContent = String(message ?? '');
  };
}

/**
 * A labelled control in the toolbar. Returns { el, control } so the caller
 * keeps a handle on the input without querying the DOM back out of it.
 *
 * `label` is a real <label for>, never a placeholder: a placeholder vanishes
 * on focus and is not an accessible name.
 */
export function field(id, labelText, control, { className = 'field' } = {}) {
  control.id = id;
  return {
    el: h('div', { class: className }, [
      h('label', { for: id }, labelText),
      control,
    ]),
    control,
  };
}

export function textInput(attrs = {}) {
  return h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', ...attrs });
}

export function select(options, attrs = {}) {
  return h('select', attrs, options.map(([value, label]) => h('option', { value }, label)));
}

/**
 * Cursor pagination (Contract 3: "cursor pagination on every list").
 *
 * There is deliberately no page-number control. The API returns an opaque
 * cursor, so a "page 7" button would be a lie — it would have to walk every
 * intervening page to build it, and the row it landed on would differ from the
 * row a second caller landed on. "Load more" is what a cursor can honestly
 * offer.
 */
export function createPagination({ id, onMore, label = 'Load more' }) {
  const button = h('button', { type: 'button', class: 'btn-sm', onClick: () => onMore() }, label);
  const note = h('span', { class: 'muted small' });
  const el = h('div', { id, class: 'approval-pagination' }, [button, note]);
  el.hidden = true;

  /**
   * @param {Object} s
   * @param {boolean} s.hasMore
   * @param {boolean} s.loading
   * @param {number} s.shown
   */
  function set({ hasMore, loading, shown }) {
    el.hidden = !hasMore && !shown;
    button.hidden = !hasMore;
    button.disabled = !!loading;
    button.textContent = loading ? 'Loading…' : label;
    note.textContent = shown
      ? `${shown} shown${hasMore ? ', more available' : ' — end of results'}`
      : '';
  }

  return { el, set };
}

/**
 * A titled section, matching the .card structure the shell already uses.
 *
 * THE HEADING IS AN <h2>, NOT AN <h3>, AND THAT IS THE WHOLE POINT.
 *
 * The shell renders the screen name as the page's <h1> (#pageTitle). The
 * frozen stylesheet's card header is `.card > h3`, so reusing it here would
 * put an h3 directly under an h1 and skip a level — axe's `heading-order`
 * flags it, and a screen-reader user navigating by heading loses the outline.
 * The budget screens hit the same wall and solved it the same way, with a
 * visible <h2> carrying a feature-stylesheet class (`.section-title` in
 * budget.css).
 *
 * `.approval-card-title` in approvals.css reproduces the approved `.card > h3`
 * appearance from the same tokens, so the card looks identical while the
 * document outline is h1 -> h2 -> h3.
 */
export function card(headingId, headingText, children, { actions = null } = {}) {
  const heading = h('h2', { id: headingId, class: 'approval-card-title' }, [
    headingText,
    actions ? h('span', { class: 'spacer' }) : null,
    actions,
  ].filter(Boolean));
  return h('section', { class: 'card approval-card', 'aria-labelledby': headingId }, [
    heading,
    h('div', { class: 'card-body' }, children),
  ]);
}

/**
 * A definition list of label/value pairs, using the frozen .kv styling.
 *
 * The element is a real <dl>. <dt> and <dd> are only meaningful inside one —
 * axe's `dlitem` rule fails them otherwise, and a screen reader announces a
 * bare list of orphaned terms instead of a description list. `.kv` in
 * styles.css sets `display: grid`, which applies to a <dl> exactly as it did
 * to the <div>; the one difference is the UA stylesheet's default `margin:
 * 1em 0` on <dl>, which `.approval-kv` in approvals.css removes so the layout
 * is unchanged.
 *
 * REPORTED: app/frontend/app.js builds `.kv` blocks as <div> in four places,
 * which carries the same dlitem defect. Those are inside client-approved shell
 * views with pixel baselines, so they are not changed here.
 */
export function keyValues(pairs) {
  const dl = h('dl', { class: 'kv approval-kv' });
  for (const [label, value] of pairs) {
    if (value === null || value === undefined) continue;
    dl.appendChild(h('dt', {}, label));
    dl.appendChild(h('dd', {}, value));
  }
  return dl;
}

/** Replace a host's children with one node. */
export function replace(host, node) {
  clear(host);
  if (node) host.appendChild(node);
}

/**
 * Read a query parameter from the current URL.
 *
 * The shell routes on the HASH, so the search string survives a hash change —
 * which is what makes `/#approval-request?…` impossible and
 * `/?instance=X#approval-request` the deep link that actually works. app.js
 * already uses exactly this pattern for the availability check's
 * `?wbs=&amt=&auto=1`.
 */
export function queryParam(name) {
  try {
    return new URLSearchParams(window.location.search).get(name) || '';
  } catch {
    return '';
  }
}
