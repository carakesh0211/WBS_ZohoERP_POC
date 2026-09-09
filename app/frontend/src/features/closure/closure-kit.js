/* app/frontend/src/features/closure/closure-kit.js
   The parts Wave 7's ten screens share, written once.

   THE RULE THIS FILE ENFORCES STRUCTURALLY: NEVER FABRICATE A TOTAL.
   -----------------------------------------------------------------
   `figure()` takes a paise value that may be `null` or `undefined`, and when
   it is, it renders an explicit "not available" state NAMING what is missing.
   It does not render `0`, it does not render `—` on its own, and it does not
   fall back to a value computed from something else on the page.

   Zero and absent are different facts. A CWIP tile reading `Rs 0.00` says the
   balance was computed and came to nothing; a tile reading "not available" says
   nobody computed it. A capitalisation decision taken against the first when
   the second was true is exactly the failure this product exists to prevent,
   and a screen cannot make that mistake if the only way to show a figure is
   through a function that refuses to invent one.

   NO ARITHMETIC ON MONEY. Every figure here is an integer paise value the
   server sent, formatted by core/format.js's formatINR. Nothing in this file
   adds, subtracts or scales a monetary value: a JavaScript float is not a safe
   container for rupees, and a total computed in the browser is a total nobody
   can reconcile against the ledger.

   Content-Security-Policy: every node is built with core/dom.js's h(), which
   throws on a `style` attribute and puts every string through a Text node. No
   selector here is unscoped and nothing here restyles the approved design.
*/

import { h } from '../../core/dom.js';
import { formatINR } from '../../core/format.js';

/** A titled card matching the approved `.card` shape, with an h2 heading.
 *
 * An h2 rather than the frozen stylesheet's `.card > h3`: the shell renders the
 * screen name as the page h1, so an h3 would skip a level -- axe flags it and a
 * screen-reader user navigating by heading loses the outline.
 */
export function card(titleId, title, body) {
  return h('section', { class: 'closure-card', 'aria-labelledby': titleId }, [
    h('h2', { id: titleId, class: 'closure-card-title' }, title),
    h('div', { class: 'closure-card-body' }, body),
  ]);
}

/**
 * One labelled money figure.
 *
 * @param {string} label
 * @param {number|null|undefined} paise - integer paise, or null/undefined when
 *   the server did not supply it.
 * @param {Object} [opts]
 * @param {string} [opts.missingReason] - what is missing, rendered VERBATIM in
 *   place of the number. Required in spirit: the default names the field, which
 *   is better than nothing but worse than a real explanation.
 */
export function figure(label, paise, { missingReason } = {}) {
  const known = typeof paise === 'number' && Number.isFinite(paise);
  return h('div', { class: 'closure-figure' }, [
    h('span', { class: 'closure-figure-label' }, label),
    known
      ? h('span', { class: 'closure-figure-value' }, formatINR(paise))
      // NOT zero, and not a bare dash. The reader is told which figure could
      // not be obtained and why, because a missing total and a nil total lead
      // to different decisions.
      : h('span', {
        class: 'closure-figure-value closure-figure-missing',
        'data-state': 'unavailable',
      }, missingReason || `${label} is not available from this build.`),
  ]);
}

/** A row of figures. */
export function figures(children) {
  return h('div', { class: 'closure-figures' }, children);
}

/**
 * Exclusion X-01, rendered.
 *
 * Shown on EVERY screen that displays a CWIP figure or a capitalisation
 * decision, above the figure, in the warning tone. This application records the
 * decision; nothing in this build posts to a general ledger or a fixed-asset
 * register, and this is the single most misreadable moment in the product.
 *
 * `status` comes off the server's row, where migration 018's
 * `ck_capitalisation_request_not_posted` pins it. It is rendered rather than
 * assumed, so a build that somehow said something else would show what it said.
 */
export function postingNote(status, note) {
  return h('p', {
    class: 'closure-posting-note',
    'data-posting-status': status || 'UNKNOWN',
  }, [
    h('strong', {}, status || 'POSTING STATUS UNKNOWN'),
    ' — ',
    note || 'This application records the capitalisation decision. No general '
      + 'ledger or fixed-asset posting exists in this build.',
  ]);
}

/**
 * The blocker list, one line per blocker, each carrying its own figure.
 *
 * Rendered from the server's array, never from splitting the server's
 * sentence: a screen that re-derives the list by splitting prose on semicolons
 * is a screen that will eventually disagree with the gate about what is
 * blocking.
 */
export function blockerList(blockers) {
  if (!Array.isArray(blockers) || blockers.length === 0) return null;
  return h('ul', { class: 'closure-blockers', 'data-blockers': String(blockers.length) },
    blockers.map((b) => h('li', {}, String(b))));
}

/**
 * A data table.
 *
 * @param {Object} config
 * @param {string} config.caption - a real <caption>, visually hidden, so the
 *   table announces what it is.
 * @param {Array<{key:string,label:string,numeric?:boolean,wrap?:boolean}>} config.columns
 * @param {Array<Object>} config.rows
 * @param {Function} [config.cell] - (row, column) -> Node|string. Defaults to
 *   the raw value, or an em dash for null.
 */
export function table({ caption, columns, rows, cell }) {
  const render = cell || ((row, col) => {
    const value = row[col.key];
    if (value === null || value === undefined || value === '') return '—';
    return String(value);
  });
  // FOCUSABLE. `overflow-x: auto` makes this a scroll container whenever the
  // table is wider than the viewport, and a scroll container that cannot take
  // focus is unreachable by keyboard -- axe `scrollable-region-focusable`,
  // raised on all six screens at 1024 and 800 and on none at 1440, because at
  // 1440 nothing overflowed. Set exactly as `capex-datatable.js:72` sets it.
  //
  // NO `role="region"`, and that was tried. Adding it made the wrapper a
  // LANDMARK, and on the three screens sharing `closure-list.js` the panel's
  // <section> and this table's caption carry the same title -- two landmarks,
  // same role, same accessible name, axe `landmark-unique`. The review is
  // right that these wrappers are unnamed tab stops, but the fix for that is a
  // name DISTINCT from the section's, not a copy of it, and that is a change
  // to the call sites rather than something to fold into this repair.
  return h('div', { class: 'closure-tablewrap', tabindex: '0' }, [
    h('table', { class: 'closure-table' }, [
      h('caption', { class: 'sr-only' }, caption),
      h('thead', {}, h('tr', {}, columns.map((col) => h(
        'th',
        { scope: 'col', class: col.numeric ? 'closure-num' : null },
        col.label,
      )))),
      h('tbody', {}, rows.map((row) => h('tr', {}, columns.map((col) => h(
        'td',
        {
          class: [col.numeric ? 'closure-num' : null,
            col.wrap ? 'closure-wrap' : null].filter(Boolean).join(' ') || null,
        },
        render(row, col),
      ))))),
    ]),
  ]);
}

/** Money for a table cell: integer paise in, tabular string out, or an em dash.
 *
 * An em dash for an absent value is correct HERE and would be wrong on a
 * `figure()`: a cell in a list is one record's field, and its absence is a
 * property of that record. A tile is a TOTAL, and a total that is absent has to
 * say so in words. */
export function money(paise) {
  return (typeof paise === 'number' && Number.isFinite(paise))
    ? formatINR(paise) : '—';
}

/** A write-off badge. Never a minus sign: a sign is not a category. */
export function writeoffBadge() {
  return h('span', { class: 'closure-writeoff' }, 'WRITE-OFF');
}

/** A labelled form field. */
export function field(id, label, control, { wide = false } = {}) {
  return h('div', {
    class: wide ? 'closure-field closure-field-wide' : 'closure-field',
  }, [
    h('label', { for: id }, label),
    control,
  ]);
}

/** A text input with the id its label points at. */
export function input(id, attrs = {}) {
  return h('input', { id, type: 'text', ...attrs });
}

/** A row of actions. */
export function actions(children) {
  return h('div', { class: 'closure-actions' }, children);
}

/** The screen root every Wave 7 screen mounts into. */
export function screen(children) {
  return h('div', { class: 'closure-screen' }, children);
}
