/* app/frontend/src/features/analytics/wbs-tree.js
   The WBS tree table SCR-06 and SCR-07 both render.

   THE ONE THING A WBS TABLE MUST NOT GET WRONG
   --------------------------------------------
   A node's `total` is its OWN value plus everything beneath it. Its `own` is
   only what was posted against the node itself. A table that shows `total` on
   every row and then sums the rows DOUBLE COUNTS every ancestor — a two-level
   project reports its budget twice, a five-level one reports parts of it five
   times, and the total looks plausible at every level.

   This component never sums rows. It renders the rollup the server computed
   for each node, and the footer — where there is one — carries the SERVER's
   project total, which is the root rollup and not an addition performed here.
   `carries_budget` is rendered explicitly as a column so a reader can see
   which rows hold budget of their own and which are showing a rollup of their
   children's.

   INDENTATION GOES THROUGH THE CSSOM
   ----------------------------------
   `main.py` sets `style-src 'self'` with no `'unsafe-inline'`, so a markup
   `style="padding-left:28px"` is blocked outright and `core/dom.js::h()`
   throws on one. Depth is applied with `setGeometry()`, which CSP does not
   govern — the same mechanism the shell's existing `enhance()` uses for tree
   indentation and bar geometry.

   EXPAND AND COLLAPSE ARE REAL BUTTONS
   ------------------------------------
   Each parent's toggle is a `<button>` with `aria-expanded`, so it is
   reachable by keyboard, announced by a screen reader and carries a visible
   focus ring from the frozen stylesheet. The rows themselves carry
   `aria-level`, `aria-expanded` and `aria-posinset` so the hierarchy survives
   into the accessibility tree rather than existing only as visual indentation.

   THE ROLE PROMISED KEYBOARD NAVIGATION THAT DID NOT EXIST
   --------------------------------------------------------
   `role="treegrid"` is not a labelling decision. It tells assistive technology
   that arrow keys move between rows, that Right expands and Left collapses,
   and that the widget manages a focus point of its own. This component
   declared the role, implemented none of it, and had no focusable row at all —
   a false promise, and a worse outcome than a plain table, because a reader
   told to press Right had nothing to press it on.

   Three defects made it concrete and all three are fixed here:

     * PRESSING A TOGGLE DESTROYED FOCUS. `onClick` called `paint()`, which
       `clear(tbody)`s and rebuilds the very button that was pressed, so
       `document.activeElement` fell back to `<body>` on every expand and
       collapse. Keyboard users lost their place in the tree each time they
       used it. The row is re-found by its `data-wbs` after the repaint and
       focus is put back where the user left it.
     * NO ROVING TABINDEX AND NO FOCUSABLE ROW. Visible rows now carry
       `tabindex="-1"` with exactly one at `0`, which is what makes a treegrid
       reachable by Tab and navigable by arrow key. The buttons and links
       INSIDE a row keep their natural tab order — a roving tabindex governs
       the rows, and taking the controls out of the tab sequence would trade
       one keyboard trap for another.
     * THE COUNT WAS ANNOUNCED FOR THE BULK BUTTONS AND NOT FOR THE TOGGLE, so
       collapsing a subtree left the live region asserting the PREVIOUS count —
       a wrong number read aloud, which is worse than silence. `onToggle` fires
       after every repaint the widget performs itself.

   The scroll wrapper is a `tabindex="0"` stop so a mouse-free reader can pan a
   wide table. A focus stop with no role and no name announces as nothing at
   all, so it carries `role="group"` and the caption as its accessible name.
*/

import { h, text, clear, setGeometry } from '../../core/dom.js';
import { statusChip } from '../../components/capex-statuschip.js';
import { inr } from './analytics-metrics.js';
import { METRIC_DEFINITION, METRIC_LABEL } from './analytics-shapes.js';
import { bandBar, bandChip } from './analytics-kit.js';
import { drilldownHref } from './analytics-filters.js';

const INDENT_PX = 14;

/**
 * Build the tree table.
 *
 * @param {Object} config
 * @param {string[]} config.metrics - money columns, in order.
 * @param {()=>Object} config.getFilters
 * @param {string} config.caption
 * @param {(row:Object)=>string} [config.rowHref] - where a WBS code links to.
 * @param {boolean} [config.showBand]
 * @param {()=>void} [config.onToggle] - called after the widget repaints
 *   itself in response to the reader, so the screen can re-announce the
 *   visible count. Without it the live region keeps asserting the count from
 *   before the expand or collapse.
 */
export function createWbsTree({
  metrics, getFilters, caption, rowHref = null, showBand = true, onToggle = null,
}) {
  const collapsed = new Set();
  let rows = [];
  /* The roving tabindex's home. One visible row holds `tabindex="0"`; every
     other holds `-1`. Kept as the WBS code rather than an index because a
     repaint renumbers the rows and the reader's place must survive it. */
  let activeWbs = null;

  const headRow = h('tr');
  headRow.appendChild(h('th', { scope: 'col', class: 'col-wbs' }, 'WBS element'));
  headRow.appendChild(h('th', { scope: 'col', class: 'col-desc' }, 'Description'));
  headRow.appendChild(h('th', { scope: 'col' }, 'Budget head'));
  headRow.appendChild(h('th', {
    scope: 'col',
    title: 'A node that carries budget holds an approved figure of its own. A node that does not '
      + 'is showing the rollup of everything beneath it — reading that as an own-value double '
      + 'counts every ancestor.',
  }, 'Holds budget'));
  headRow.appendChild(h('th', { scope: 'col' }, 'Status'));
  for (const key of metrics) {
    headRow.appendChild(h('th', {
      scope: 'col',
      class: 'num',
      title: METRIC_DEFINITION[key] || '',
    }, METRIC_LABEL[key] || key));
  }
  if (showBand) headRow.appendChild(h('th', { scope: 'col' }, 'Utilisation'));

  const tbody = h('tbody');
  /* `treegrid`, NOT the implicit `table` role.
     Every row below carries `aria-level`, `aria-posinset`, `aria-setsize` and
     — on a parent — `aria-expanded`, which is how the hierarchy survives for
     anyone not looking at the indentation. Those four attributes are only
     valid on a `row` that a TREEGRID owns; on a plain table they are a
     serious `aria-conditional-attr` violation, one per row, and axe reported
     exactly that once these trees became visible (they were rendering into a
     hidden element, so nothing had ever audited them).
     Declaring the role is the fix rather than dropping the attributes: a tree
     table whose depth exists only in pixels is a flat list to a screen reader,
     which is the thing `tests/vrt/analytics.spec.js` asserts against. */
  const table = h('table', { role: 'treegrid' }, [
    h('caption', { class: 'sr-only' }, caption),
    h('thead', {}, headRow),
    tbody,
  ]);
  /* A `tabindex="0"` scroll container with no role and no accessible name is a
     tab stop that announces nothing — the reader is told only "group" or, on
     some combinations, nothing at all. The stop itself is right (a wide table
     must be pannable without a mouse; axe's `scrollable-region-focusable` says
     so), so it is NAMED rather than removed. */
  const wrap = h('div', {
    class: 'table-wrap',
    tabindex: '0',
    role: 'group',
    'aria-label': caption,
  }, table);
  const columnCount = 5 + metrics.length + (showBand ? 1 : 0);

  /** Is this row hidden because one of its ancestors is collapsed? */
  function hiddenByAncestor(index) {
    const row = rows[index];
    for (let i = index - 1; i >= 0; i -= 1) {
      const candidate = rows[i];
      if (candidate.depth < row.depth) {
        if (collapsed.has(candidate.wbs_id)) return true;
        if (candidate.depth === 0) return false;
      }
    }
    return false;
  }

  function moneyCell(row, key) {
    const value = row[key];
    if (value === null || value === undefined) {
      return h('span', {
        class: 'analytics-not-reported',
        title: `The source did not report ${METRIC_LABEL[key] || key} for this element. It is `
          + 'not zero.',
      }, [h('span', { class: 'sym', 'aria-hidden': 'true' }, '?'), text(' not reported')]);
    }
    const filters = getFilters();
    if (!row.wbs_code) return text(inr(value));
    return h('a', {
      class: 'linkish analytics-drill',
      'data-metric': key,
      href: drilldownHref(
        key === 'actual' || key === 'received' ? 'analytics-cwip-ledger'
          : key === 'commitment' || key === 'ordered' ? 'analytics-commitment-ageing'
            : 'analytics-wbs-element',
        filters,
        {
          metric: key,
          dimension: 'wbs',
          narrowKey: 'wbs_paths',
          narrowValue: row.wbs_code,
        },
      ),
      title: `${inr(value)} — open the source records behind ${METRIC_LABEL[key] || key} for `
        + `${row.wbs_code}. The drill-down carries this screen's filters plus this subtree.`,
    }, inr(value));
  }

  function renderRow(row, index, posinset, setsize) {
    const tr = h('tr', {
      class: 'analytics-tree-row',
      role: 'row',
      'aria-level': String(row.depth + 1),
      'aria-posinset': String(posinset),
      'aria-setsize': String(setsize),
      'data-wbs': row.wbs_code || '',
      'data-depth': String(row.depth),
      'data-carries-budget': String(row.carries_budget),
      // The roving tabindex. Exactly one visible row carries 0; see paint().
      tabindex: '-1',
    });
    if (row.has_children) tr.setAttribute('aria-expanded', String(!collapsed.has(row.wbs_id)));

    const spacer = h('span', { class: 'analytics-tree-indent', 'aria-hidden': 'true' });
    // CSSOM, not a style attribute: the CSP blocks the attribute and h() throws.
    setGeometry(spacer, { width: `${row.depth * INDENT_PX}px` });

    const toggle = row.has_children
      ? h('button', {
        type: 'button',
        class: 'tree-toggle',
        'aria-expanded': String(!collapsed.has(row.wbs_id)),
        'aria-label': `${collapsed.has(row.wbs_id) ? 'Expand' : 'Collapse'} ${row.wbs_code || 'this element'}`,
        onClick: () => toggleNode(row, { restoreFocusTo: 'toggle' }),
      }, collapsed.has(row.wbs_id) ? '▸' : '▾')
      : h('span', { class: 'tree-toggle leaf', 'aria-hidden': 'true' }, '▸');
    if (row.has_children) toggle.setAttribute('data-wbs', row.wbs_code || '');

    const code = rowHref && row.wbs_id
      ? h('a', { class: 'linkish mono analytics-tree-code', href: rowHref(row) }, String(row.wbs_code || row.wbs_id))
      : h('span', { class: 'mono analytics-tree-code' }, String(row.wbs_code || '—'));

    tr.appendChild(h('th', { scope: 'row', class: 'wbs-cell' },
      h('span', { class: 'analytics-tree-cell' }, [spacer, toggle, code])));
    tr.appendChild(h('td', { class: 'col-desc' }, text(row.description || '—')));
    tr.appendChild(h('td', {}, text(row.budget_head || '—')));
    tr.appendChild(h('td', {}, row.carries_budget
      ? statusChip({
        label: 'OWN BUDGET',
        tone: 'positive',
        title: 'This element holds an approved budget of its own.',
      })
      : statusChip({
        label: 'ROLLUP',
        tone: 'neutral',
        title: row.budget_owner_code
          ? `This element holds no budget of its own; budget is held at ${row.budget_owner_code}. `
            + 'The figures on this row are the rollup of everything beneath it.'
          : 'This element holds no budget of its own. The figures on this row are the rollup of '
            + 'everything beneath it — adding them to its children would double count.',
      })));
    tr.appendChild(h('td', {}, row.status
      ? statusChip({ label: String(row.status), tone: 'neutral', title: `Lifecycle status: ${row.status}.` })
      : h('span', { class: 'muted' }, '—')));
    for (const key of metrics) tr.appendChild(h('td', { class: 'num' }, moneyCell(row, key)));
    if (showBand) {
      tr.appendChild(h('td', {}, h('div', { class: 'analytics-band-cell' }, [
        bandChip(row.utilisation_bp),
        bandBar({
          actualBp: row.actual_bp,
          commitmentBp: row.utilisation_bp,
          label: `Exposure against budget for ${row.wbs_code || 'this element'}.`,
        }),
      ])));
    }
    return tr;
  }

  function paint() {
    clear(tbody);
    if (!rows.length) {
      tbody.appendChild(h('tr', {}, h('td', { colspan: String(columnCount) },
        h('div', { class: 'empty' }, [
          h('div', { class: 'big', 'aria-hidden': 'true' }, '⌗'),
          h('div', {}, 'This project has no WBS element matching these filters.'),
        ]))));
      return;
    }
    /* Sibling counts, for aria-posinset / aria-setsize. Computed per parent so
       a screen reader can say "3 of 7" rather than "3 of the whole tree". */
    const siblingIndex = new Map();
    const siblingCount = new Map();
    for (const row of rows) {
      const key = `${row.depth}:${row.parentKey || ''}`;
      siblingCount.set(key, (siblingCount.get(key) || 0) + 1);
    }
    rows.forEach((row, index) => {
      if (hiddenByAncestor(index)) return;
      const key = `${row.depth}:${row.parentKey || ''}`;
      const pos = (siblingIndex.get(key) || 0) + 1;
      siblingIndex.set(key, pos);
      tbody.appendChild(renderRow(row, index, pos, siblingCount.get(key) || 1));
    });
    applyRovingTabindex();
  }

  /* ---------------------------------------------------------------- *
   * Focus: a treegrid manages a focus point, and this is it
   * ---------------------------------------------------------------- */

  /** Every row currently in the DOM, in visual order. */
  function visibleRowEls() {
    return [...tbody.querySelectorAll('tr.analytics-tree-row')];
  }

  /**
   * Give exactly one visible row `tabindex="0"`.
   *
   * A treegrid with no `tabindex="0"` row cannot be reached by Tab at all, and
   * one with several is a widget the reader has to tab THROUGH rather than
   * INTO. The stop follows `activeWbs` when that row is still visible — a
   * reader who collapsed an ancestor keeps their place at the ancestor rather
   * than being thrown back to the top of the tree.
   */
  function applyRovingTabindex() {
    const els = visibleRowEls();
    if (!els.length) return;
    let home = activeWbs
      ? els.find((el) => el.getAttribute('data-wbs') === activeWbs)
      : null;
    if (!home) [home] = els;
    for (const el of els) el.setAttribute('tabindex', el === home ? '0' : '-1');
    activeWbs = home.getAttribute('data-wbs') || activeWbs;
  }

  function rowElFor(wbsCode) {
    if (!wbsCode) return null;
    return visibleRowEls().find((el) => el.getAttribute('data-wbs') === wbsCode) || null;
  }

  /**
   * Put focus back after a repaint.
   *
   * `paint()` clears the tbody, so the element that was focused no longer
   * exists — the browser moves focus to `<body>` and the reader is silently
   * ejected from the widget. The row is re-found by `data-wbs`, which survives
   * the rebuild because it is data rather than identity.
   *
   * @param {string} wbsCode
   * @param {'toggle'|'row'} where - the pressed control if it still exists,
   *   else the row itself. A leaf has no toggle, and a toggle that collapsed
   *   its own subtree still does.
   */
  function restoreFocus(wbsCode, where) {
    const rowEl = rowElFor(wbsCode);
    if (!rowEl) return;
    activeWbs = wbsCode;
    applyRovingTabindex();
    const target = where === 'toggle'
      ? rowEl.querySelector('button.tree-toggle') || rowEl
      : rowEl;
    try { target.focus(); } catch { /* detached between paint and focus */ }
  }

  /**
   * Expand or collapse one node, repaint, and put the reader back where they
   * were — then tell the screen, so the announced count is the count that is
   * now on screen rather than the one that was.
   */
  function toggleNode(row, { restoreFocusTo = 'row' } = {}) {
    if (!row || !row.has_children) return;
    if (collapsed.has(row.wbs_id)) collapsed.delete(row.wbs_id);
    else collapsed.add(row.wbs_id);
    paint();
    restoreFocus(row.wbs_code || '', restoreFocusTo);
    if (onToggle) onToggle();
  }

  /** The model row behind a rendered `<tr>`. */
  function rowDataFor(el) {
    const code = el && el.getAttribute('data-wbs');
    if (!code) return null;
    return rows.find((r) => (r.wbs_code || '') === code) || null;
  }

  function focusRowEl(el) {
    if (!el) return;
    activeWbs = el.getAttribute('data-wbs') || activeWbs;
    applyRovingTabindex();
    try { el.focus(); } catch { /* detached */ }
  }

  /**
   * The keyboard interaction `role="treegrid"` promises.
   *
   * Up/Down walk the VISIBLE rows — a collapsed subtree is not on screen and
   * must not be arrowed into. Right expands a closed node and steps into an
   * open one; Left collapses an open node and steps out to the parent of a
   * closed or childless one. Home and End jump to the ends. Everything else,
   * including Tab, Enter and Space, is left alone: the toggle is a real button
   * and activating it is the button's job, not this handler's.
   */
  function onKeyDown(event) {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const current = event.target && event.target.closest
      ? event.target.closest('tr.analytics-tree-row')
      : null;
    if (!current || !tbody.contains(current)) return;
    const els = visibleRowEls();
    const at = els.indexOf(current);
    if (at < 0) return;
    const data = rowDataFor(current);
    const depth = Number(current.getAttribute('data-depth') || '0');
    const expanded = current.getAttribute('aria-expanded');

    let handled = true;
    switch (event.key) {
      case 'ArrowDown':
        focusRowEl(els[Math.min(at + 1, els.length - 1)]);
        break;
      case 'ArrowUp':
        focusRowEl(els[Math.max(at - 1, 0)]);
        break;
      case 'Home':
        focusRowEl(els[0]);
        break;
      case 'End':
        focusRowEl(els[els.length - 1]);
        break;
      case 'ArrowRight':
        if (expanded === 'false') toggleNode(data, { restoreFocusTo: 'row' });
        else if (expanded === 'true' && els[at + 1]) focusRowEl(els[at + 1]);
        else handled = false;
        break;
      case 'ArrowLeft':
        if (expanded === 'true') {
          toggleNode(data, { restoreFocusTo: 'row' });
        } else {
          // Step OUT: the nearest row above at a shallower depth is the parent.
          const parent = els.slice(0, at).reverse()
            .find((el) => Number(el.getAttribute('data-depth') || '0') < depth);
          if (parent) focusRowEl(parent); else handled = false;
        }
        break;
      default:
        handled = false;
    }
    /* Only a key this widget actually acted on is swallowed. Preventing the
       default on every key would break Tab out of the tree and the browser's
       own find-as-you-type. */
    if (handled) event.preventDefault();
  }

  tbody.addEventListener('keydown', onKeyDown);

  /** Skeleton rows, matching the shared table component's treatment. */
  function renderSkeleton() {
    clear(tbody);
    for (let i = 0; i < 6; i += 1) {
      const tr = h('tr', { class: 'audit-skel-row', 'aria-hidden': 'true' });
      for (let c = 0; c < columnCount; c += 1) {
        const bar = h('span', { class: 'audit-skel-bar' });
        setGeometry(bar, { width: `${35 + ((i * 13 + c * 7) % 55)}%` });
        tr.appendChild(h('td', {}, bar));
      }
      tbody.appendChild(tr);
    }
  }

  /**
   * @param {Array} next - rows from `flattenWbs()`.
   * @param {Object} [opts]
   * @param {number} [opts.collapseBelow] - collapse every node deeper than
   *   this. An explorer opens shallow; a tree table opens flat.
   */
  function setRows(next, { collapseBelow = null } = {}) {
    rows = (next || []).map((row, index, all) => {
      // Remember the parent's code, so sibling counting does not need a second
      // walk of the nested structure.
      let parentKey = '';
      for (let i = index - 1; i >= 0; i -= 1) {
        if (all[i].depth < row.depth) { parentKey = all[i].wbs_code || all[i].wbs_id || ''; break; }
      }
      return { ...row, parentKey };
    });
    // A new hierarchy is a new set of rows; the previous project's focus point
    // means nothing in it, and keeping it would put the tab stop on a code that
    // is no longer here.
    activeWbs = null;
    collapsed.clear();
    if (collapseBelow !== null) {
      for (const row of rows) {
        if (row.has_children && row.depth >= collapseBelow) collapsed.add(row.wbs_id);
      }
    }
    paint();
  }

  function expandAll() { collapsed.clear(); paint(); }

  function collapseAll(depth = 0) {
    collapsed.clear();
    for (const row of rows) if (row.has_children && row.depth >= depth) collapsed.add(row.wbs_id);
    paint();
  }

  /** How many rows are currently visible — for the announcer, not for a total. */
  function visibleCount() {
    return rows.filter((_, index) => !hiddenByAncestor(index)).length;
  }

  return { el: wrap, setRows, renderSkeleton, expandAll, collapseAll, visibleCount, paint };
}
