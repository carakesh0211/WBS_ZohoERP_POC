/* app/frontend/src/core/dom.js
   Safe element construction for the ES-module screens (SCR-nn).

   The server sends Content-Security-Policy: style-src 'self' with no
   'unsafe-inline', so a markup `style="…"` attribute is BLOCKED outright.
   CSSOM assignment (el.style.prop = …) is NOT governed by that policy at all.
   h() enforces the distinction by refusing an attrs.style value; use
   setGeometry() for anything that must be computed at runtime (bar widths,
   skeleton-bar variety, tree indentation).

   Every interpolation into the DOM here goes through textContent / a Text
   node — never innerHTML — so escaping is structural, not a rule someone has
   to remember to apply per call site.
*/

/**
 * Create an element.
 * @param {string} tag
 * @param {Object} [attrs] - HTML attributes. `class` sets className. A
 *   function value under an `on*` key is added as an event listener
 *   (removing the `on` prefix, lower-cased: onClick -> 'click'). `dataset`
 *   takes a plain object and is applied via el.dataset. `style` is forbidden
 *   and throws — see setGeometry().
 * @param {Array|Node|string|number} [children]
 */
export function h(tag, attrs = {}, children = []) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'style') {
      throw new Error(
        'h(): a "style" attribute is forbidden by the CSP (style-src \'self\'). ' +
        'Use setGeometry(el, props) to assign computed geometry through the CSSOM instead.',
      );
    }
    if (key === 'class') { el.className = value; continue; }
    if (key === 'dataset') {
      for (const [dataKey, dataValue] of Object.entries(value)) el.dataset[dataKey] = dataValue;
      continue;
    }
    if (key.startsWith('on') && typeof value === 'function') {
      el.addEventListener(key.slice(2).toLowerCase(), value);
      continue;
    }
    if (value === true) { el.setAttribute(key, ''); continue; }
    el.setAttribute(key, String(value));
  }
  appendChildren(el, children);
  return el;
}

function appendChildren(el, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) { appendChildren(el, child); continue; }
    if (child instanceof Node) { el.appendChild(child); continue; }
    el.appendChild(text(child));
  }
}

/** A Text node — the only way strings ever reach the DOM in this codebase. */
export function text(value) {
  return document.createTextNode(String(value ?? ''));
}

/** Remove every child of `el`. */
export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
}

/**
 * Assign computed geometry through the CSSOM — the one path CSP does not
 * govern. Never build a `style="…"` string; call this instead.
 * @param {HTMLElement} el
 * @param {Object} props - camelCase CSS property names to values, e.g.
 *   { width: '42%', left: '0' }.
 */
export function setGeometry(el, props) {
  for (const [prop, value] of Object.entries(props || {})) {
    el.style[prop] = value;
  }
}
