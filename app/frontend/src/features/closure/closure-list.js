/* app/frontend/src/features/closure/closure-list.js
   The read-only list screen, written once for SCR-11, SCR-12, SCR-14 and
   SCR-40.

   Four screens with the same shape -- a filter, a load, a table, and the six
   states -- is four chances for one of them to render a refusal as an empty
   list. Wave 2 produced exactly that: four hand-rolled copies of the same four
   states, two of which rendered a 403 as an error box. This is the one
   implementation, and each screen supplies only its columns, its wording and
   its call.

   THE SIX STATES, AND THAT THEY ARE SIX
   -------------------------------------
   loading / empty / unavailable / error / permission-denied / success, driven
   by features/integration/integration-screen.js's `createLoader`, which
   already models all six and is proved by tests/vrt/integration.spec.js.
   UNAVAILABLE is the one that is easy to lose: it is NOT empty ("no records
   were found" is false when nothing was asked), NOT error (nothing failed) and
   NOT permission (the operator's rights are not the reason). It says which
   route is missing, by name.

   THE TABLE IS HIDDEN IN EVERY STATE BUT SUCCESS. A stale table left under an
   error banner is a reader looking at last minute's numbers while being told
   this minute's could not be fetched.
*/

import { h } from '../../core/dom.js';
import { createLoader } from '../integration/integration-screen.js';
import { createAnnouncer, queryParam } from '../integration/integration-kit.js';
import { actions, card, field, input, screen, table } from './closure-kit.js';

/**
 * @param {Object} config
 * @param {HTMLElement} config.root
 * @param {string} config.id            - unique prefix for element ids.
 * @param {string} config.title
 * @param {string} config.intro         - one paragraph saying what this is.
 * @param {string} config.what          - sentence fragment for the loader.
 * @param {string} config.emptyMessage
 * @param {string} config.glyph
 * @param {Array} config.columns        - closure-kit `table()` columns.
 * @param {Function} config.cell        - (row, column) -> Node|string.
 * @param {Function} config.call        - (params) -> the api envelope.
 * @param {Array<{name:string,label:string,placeholder?:string}>} [config.filters]
 * @param {Function} [config.summary]   - (rows, envelope) -> Node|null, drawn
 *   above the table. Anything it returns must come from the SERVER's own
 *   fields; it must not compute a total.
 */
export function createListScreen({
  root, id, title, intro, what, emptyMessage, glyph = '·',
  columns, cell, call, filters = [], summary = null,
}) {
  if (!root) return null;
  const announce = createAnnouncer('closureLiveRegion');
  const inputs = new Map();

  for (const f of filters) {
    inputs.set(f.name, input(`${id}-${f.name}`, {
      placeholder: f.placeholder || '',
      value: queryParam(f.name) || '',
    }));
  }

  const body = h('div');
  const summaryHost = h('div');

  const loader = createLoader({
    id: `${id}Status`,
    glyph,
    what,
    emptyMessage,
    loadingMessage: `Loading ${what.toLowerCase()}…`,
    onRetry: () => load(),
    announce,
  });

  const toolbar = filters.length
    ? h('div', { class: 'closure-form', role: 'group', 'aria-label': `${title} filters` }, [
      ...filters.map((f) => field(`${id}-${f.name}`, f.label, inputs.get(f.name))),
      actions([h('button', {
        type: 'button', class: 'btn-primary btn-sm', onClick: () => load(),
      }, 'Apply')]),
    ])
    : null;

  root.appendChild(screen([
    card(`${id}Title`, title, [
      h('p', { class: 'muted small' }, intro),
      toolbar,
      loader.el,
      summaryHost,
      body,
    ].filter(Boolean)),
  ]));

  function params() {
    const out = {};
    for (const [name, el] of inputs) {
      const value = String(el.value || '').trim();
      if (value) out[name] = value;
    }
    return out;
  }

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  async function load() {
    await loader.run(() => call(params()), {
      render: (data, envelope) => {
        clear(body);
        clear(summaryHost);
        const rows = (data && Array.isArray(data.items)) ? data.items : [];
        if (!rows.length) return false;
        if (summary) {
          const node = summary(rows, envelope, data);
          if (node) summaryHost.appendChild(node);
        }
        body.appendChild(table({ caption: title, columns, rows, cell }));
        return true;
      },
      onState: (state) => {
        // The table exists only in `ready`. See the header.
        body.hidden = state !== 'ready';
        summaryHost.hidden = state !== 'ready';
      },
    });
  }

  load();
  return { load };
}
