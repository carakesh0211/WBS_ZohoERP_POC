/* app/frontend/src/features/analytics/analytics-filters.js
   ONE FilterSet, for cards, charts, tables, drill-downs and exports.

   WHY THERE IS EXACTLY ONE
   ------------------------
   The Wave 7 contract states it as a rule and then explains the failure it
   prevents: "A second filter shape is how a dashboard card and its own
   drill-down come to disagree." A card that says ₹4.2 crore and a drill-down
   that lists ₹3.9 crore of rows is not a display bug — it is two different
   questions being asked of the server and only one of them being labelled. So
   every screen in this feature builds ONE object here, hands the SAME object
   to its cards, its table, its drill-down and its export, and never
   reconstructs a second one from what happens to be in an input box.

   THE URL IS THE FILTER STATE, NOT A COPY OF IT
   ---------------------------------------------
   Every screen must deep-link on a COLD LOAD — a pasted URL, a bookmark, a
   link in an email — and arrive showing the same figures the sender saw. That
   only holds if the filters live in the URL and the screen reads them from
   there at mount, rather than keeping them in a module variable that a fresh
   page load starts empty.

   The shell routes on the HASH, so the SEARCH string survives navigation
   between screens: `/?project=P-01&from=2026-04-01#analytics-cwip-ledger`.
   That is the same mechanism the approval screens use for `?instance=` and
   the integration screens for `?connection=`, so a link built here behaves
   like every other link in the application.

   The consequence that matters for the drill-down contract: a drill-down is a
   URL. `drilldownHref()` produces the SAME FilterSet plus the clicked
   dimension, which means the drill-down is bookmarkable, is auditable, and —
   most importantly — cannot silently differ from the card, because there is
   only one serialisation and both sides use it.

   SCOPE IS NOT A FILTER
   ---------------------
   Nothing here narrows or widens what the caller may see. A FilterSet narrows
   WITHIN the caller's server-resolved scope; an entity id typed in here that
   the caller holds no grant for yields no rows, from the server, and never an
   error that would confirm the entity exists. This module is a query builder
   and has no opinion about authorisation.
*/

import { h, text } from '../../core/dom.js';

/**
 * The canonical FilterSet, verbatim from the Wave 7 contract.
 *
 * `type` drives parsing and serialisation and nothing else. `list` keys are
 * comma-separated in the URL and arrive as arrays; `scalar` keys are single
 * values. `label` is what a chip says when the filter is active, so a reader
 * can see at a glance which nine dimensions are narrowing the figure in front
 * of them.
 */
export const FILTER_FIELDS = Object.freeze([
  { key: 'entity_ids', param: 'entity', type: 'list', label: 'Entity' },
  { key: 'plant_ids', param: 'plant', type: 'list', label: 'Plant' },
  { key: 'location_ids', param: 'location', type: 'list', label: 'Location' },
  { key: 'project_ids', param: 'project', type: 'list', label: 'Project' },
  /* ltree SUBTREE PATHS, not ids. A WBS filter selects a branch and
     everything under it; sending ids would select the nodes named and silently
     drop their children, which on a rollup screen understates every parent. */
  { key: 'wbs_paths', param: 'wbs', type: 'list', label: 'WBS subtree' },
  { key: 'budget_head_ids', param: 'head', type: 'list', label: 'Budget head' },
  /* AMB-04: budget head is the PRIMARY reading and category is secondary.
     Both are offered; the label says which is which so a reader does not
     assume they are interchangeable. */
  { key: 'category_ids', param: 'category', type: 'list', label: 'Category (secondary to budget head)' },
  { key: 'vendor_ids', param: 'vendor', type: 'list', label: 'Vendor' },
  { key: 'item_ids', param: 'item', type: 'list', label: 'Item' },
  { key: 'document_types', param: 'doctype', type: 'list', label: 'Document type' },
  { key: 'lifecycle_statuses', param: 'lifecycle', type: 'list', label: 'Lifecycle status' },
  { key: 'approval_statuses', param: 'approval', type: 'list', label: 'Approval status' },
  { key: 'date_from', param: 'from', type: 'scalar', label: 'From' },
  { key: 'date_to', param: 'to', type: 'scalar', label: 'To' },
  /* An ACCOUNTING PERIOD, not a raw date range. The two are different
     questions — a period is closed or open and a date range is neither — and
     a report run over a range that straddles a close is not the report
     finance signs. Both exist; neither substitutes for the other. */
  { key: 'period_ids', param: 'period', type: 'list', label: 'Accounting period' },
  { key: 'group_by', param: 'group', type: 'list', label: 'Grouped by' },
  { key: 'cursor', param: 'cursor', type: 'scalar', label: 'Page' },
  { key: 'limit', param: 'limit', type: 'scalar', label: 'Page size' },
  { key: 'sort', param: 'sort', type: 'scalar', label: 'Sort' },
]);

const BY_PARAM = new Map(FILTER_FIELDS.map((f) => [f.param, f]));
const BY_KEY = new Map(FILTER_FIELDS.map((f) => [f.key, f]));

/** The document types C15/C3 recognise, offered as a filter. */
export const DOCUMENT_TYPES = Object.freeze(['PR', 'PO', 'GRN', 'BILL']);

/** The dimensions a report may be grouped by, and drilled down into. */
export const GROUP_DIMENSIONS = Object.freeze([
  { key: 'entity', label: 'Entity' },
  { key: 'plant', label: 'Plant' },
  { key: 'location', label: 'Location' },
  { key: 'project', label: 'Project' },
  { key: 'wbs', label: 'WBS element' },
  { key: 'budget_head', label: 'Budget head' },
  { key: 'category', label: 'Category' },
  { key: 'vendor', label: 'Vendor' },
  { key: 'period', label: 'Accounting period' },
]);

/** An empty FilterSet with every key present, so no consumer sees `undefined`. */
export function emptyFilterSet() {
  const out = {};
  for (const f of FILTER_FIELDS) out[f.key] = f.type === 'list' ? [] : null;
  return out;
}

/**
 * Read a FilterSet out of a URL search string.
 *
 * Defaults to `window.location.search`, which is what makes a COLD LOAD of a
 * pasted link show the sender's figures rather than an unfiltered portfolio.
 * A malformed search string yields an empty FilterSet rather than throwing —
 * a bad bookmark should show the whole portfolio, not a broken screen.
 */
export function readFilters(search) {
  const out = emptyFilterSet();
  let params;
  try {
    params = new URLSearchParams(
      search !== undefined && search !== null ? search : window.location.search,
    );
  } catch {
    return out;
  }
  for (const [param, raw] of params.entries()) {
    const field = BY_PARAM.get(param);
    if (!field) continue;
    const value = String(raw).trim();
    if (!value) continue;
    if (field.type === 'list') {
      const parts = value.split(',').map((s) => s.trim()).filter(Boolean);
      // Repeated keys accumulate rather than overwrite: ?plant=A&plant=B is
      // the same request as ?plant=A,B and must not silently drop A.
      out[field.key] = [...new Set([...out[field.key], ...parts])];
    } else {
      out[field.key] = value;
    }
  }
  return out;
}

/**
 * Serialise a FilterSet back to a search string (without the leading '?').
 *
 * Keys are emitted in FILTER_FIELDS order, and list values in the order they
 * were given, so the SAME FilterSet always produces the SAME string. That
 * determinism is not cosmetic: two links that differ only in key order are two
 * cache entries, two audit records and two things a reader has to compare by
 * eye to decide whether they are the same report.
 */
export function writeFilters(filters, extra = {}) {
  const params = new URLSearchParams();
  for (const field of FILTER_FIELDS) {
    const value = (filters || {})[field.key];
    if (value === null || value === undefined || value === '') continue;
    if (field.type === 'list') {
      if (!Array.isArray(value) || !value.length) continue;
      params.set(field.param, value.join(','));
    } else {
      params.set(field.param, String(value));
    }
  }
  for (const [key, value] of Object.entries(extra || {})) {
    if (value === null || value === undefined || value === '') continue;
    params.set(key, String(value));
  }
  return params.toString();
}

/** True when no dimension is narrowing anything. */
export function isEmptyFilterSet(filters) {
  return !FILTER_FIELDS.some((f) => {
    const v = (filters || {})[f.key];
    return Array.isArray(v) ? v.length > 0 : (v !== null && v !== undefined && v !== '');
  });
}

/** The active dimensions, as `{key, label, value}` — for chips and for prose. */
export function activeFilters(filters) {
  const out = [];
  for (const field of FILTER_FIELDS) {
    if (field.key === 'cursor' || field.key === 'limit' || field.key === 'sort') continue;
    const v = (filters || {})[field.key];
    if (Array.isArray(v)) {
      if (v.length) out.push({ key: field.key, label: field.label, value: v.join(', ') });
    } else if (v !== null && v !== undefined && v !== '') {
      out.push({ key: field.key, label: field.label, value: String(v) });
    }
  }
  return out;
}

/** A human label for a FilterSet key — used when naming what was NOT applied. */
export function labelFor(key) {
  const field = BY_KEY.get(key);
  return field ? field.label : key;
}

/**
 * A deep link to `screenId` carrying THIS FilterSet.
 *
 * The search string survives the hash change, so the target screen's
 * `readFilters()` sees exactly what this screen held.
 */
export function screenHref(screenId, filters, extra = {}) {
  const q = writeFilters(filters, extra);
  return `/${q ? `?${q}` : ''}#${screenId}`;
}

/**
 * A drill-down link: the SAME FilterSet plus the clicked dimension.
 *
 * `metric` says which figure was clicked and `dimension` says which slice of
 * it, and both travel in the URL so the drill-down is reproducible by anyone
 * the link is sent to. Additional narrowing — the clicked bar's own value —
 * goes in as a real FilterSet key so the target screen filters on it rather
 * than displaying it as a caption over unfiltered rows.
 *
 * @param {string} screenId
 * @param {Object} filters - the card's FilterSet, unchanged.
 * @param {Object} clicked
 * @param {string} clicked.metric - e.g. 'actual', 'commitment'.
 * @param {string} [clicked.dimension] - e.g. 'project'.
 * @param {string} [clicked.narrowKey] - a FILTER_FIELDS key to add.
 * @param {string} [clicked.narrowValue]
 */
export function drilldownHref(screenId, filters, clicked = {}) {
  const next = { ...filters };
  if (clicked.narrowKey && clicked.narrowValue) {
    const field = BY_KEY.get(clicked.narrowKey);
    if (field && field.type === 'list') {
      const current = Array.isArray(next[field.key]) ? next[field.key] : [];
      next[field.key] = [...new Set([...current, String(clicked.narrowValue)])];
    } else if (field) {
      next[field.key] = String(clicked.narrowValue);
    }
  }
  // A drill-down starts at the first page of its own result, never inheriting
  // the card screen's cursor — which would page into the middle of a different
  // result set and produce rows that cannot sum back to anything.
  next.cursor = null;
  return screenHref(screenId, next, {
    metric: clicked.metric || undefined,
    dim: clicked.dimension || undefined,
  });
}

/** Read the drill-down context a `drilldownHref()` put in the URL. */
export function readDrilldownContext(search) {
  try {
    const params = new URLSearchParams(
      search !== undefined && search !== null ? search : window.location.search,
    );
    return {
      metric: params.get('metric') || null,
      dimension: params.get('dim') || null,
    };
  } catch {
    return { metric: null, dimension: null };
  }
}

/* ------------------------------------------------------------------ *
 * The filter bar
 * ------------------------------------------------------------------ */

/**
 * The composable filter bar, built from FILTER_FIELDS so it can never offer a
 * dimension the FilterSet does not carry — or omit one it does.
 *
 * Only the dimensions a given screen can actually use are rendered; the rest
 * still round-trip through the URL untouched, so a link that carries a vendor
 * filter into a screen with no vendor input does not lose it on the way to the
 * next screen. Dropping an unknown filter would be a silent widening of the
 * query, which is the one direction a filter must never move on its own.
 *
 * Every control is a real labelled element with a visible focus ring, and the
 * bar is a `role="group"` with a name, so a keyboard user is told what these
 * controls belong to.
 *
 * @param {Object} config
 * @param {string} config.idPrefix - unique per screen, so two mounted screens
 *   cannot collide on a label's `for`.
 * @param {string[]} config.fields - FILTER_FIELDS keys to render.
 * @param {Object} config.value - the current FilterSet.
 * @param {(next:Object)=>void} config.onApply
 * @param {Array<{key:string,label:string}>} [config.groupOptions]
 */
export function createFilterBar({
  idPrefix, fields, value, onApply, groupOptions = null,
}) {
  const current = { ...emptyFilterSet(), ...(value || {}) };
  const controls = new Map();

  function inputFor(field) {
    const id = `${idPrefix}-${field.param}`;
    let control;
    if (field.key === 'document_types') {
      control = h('select', { id, multiple: true, size: '4' },
        DOCUMENT_TYPES.map((t) => h('option', {
          value: t, selected: (current.document_types || []).includes(t),
        }, t)));
    } else if (field.key === 'group_by' && groupOptions) {
      control = h('select', { id, multiple: true, size: '4' },
        groupOptions.map((o) => h('option', {
          value: o.key, selected: (current.group_by || []).includes(o.key),
        }, o.label)));
    } else if (field.key === 'date_from' || field.key === 'date_to') {
      control = h('input', { id, type: 'date', value: current[field.key] || '' });
    } else {
      const v = current[field.key];
      control = h('input', {
        id,
        type: 'text',
        autocomplete: 'off',
        spellcheck: 'false',
        value: Array.isArray(v) ? v.join(',') : (v || ''),
      });
    }
    controls.set(field.key, control);
    const hintId = `${id}-hint`;
    const hint = field.type === 'list' && control.tagName === 'INPUT'
      ? 'Comma-separate several values.'
      : null;
    if (hint) control.setAttribute('aria-describedby', hintId);
    return h('div', { class: 'field analytics-filter-field' }, [
      h('label', { for: id }, field.label),
      control,
      hint ? h('div', { id: hintId, class: 'xs muted' }, hint) : null,
    ].filter(Boolean));
  }

  /** Read every rendered control back into a FilterSet, preserving the rest. */
  function collect() {
    const next = { ...current };
    for (const [key, control] of controls) {
      const field = BY_KEY.get(key);
      if (control.tagName === 'SELECT' && control.multiple) {
        next[key] = [...control.selectedOptions].map((o) => o.value);
        continue;
      }
      const raw = String(control.value || '').trim();
      if (field.type === 'list') {
        next[key] = raw ? raw.split(',').map((s) => s.trim()).filter(Boolean) : [];
      } else {
        next[key] = raw || null;
      }
    }
    // A filter change starts a new result. Carrying the old cursor forward
    // would page into a result set that no longer exists.
    next.cursor = null;
    return next;
  }

  const rendered = (fields || [])
    .map((key) => BY_KEY.get(key))
    .filter(Boolean)
    .map(inputFor);

  const applyBtn = h('button', {
    type: 'button', class: 'btn-primary btn-sm',
    onClick: () => onApply(collect()),
  }, 'Apply filters');

  const clearBtn = h('button', {
    type: 'button', class: 'btn-sm',
    onClick: () => {
      for (const [key, control] of controls) {
        if (control.tagName === 'SELECT' && control.multiple) {
          for (const option of control.options) option.selected = false;
        } else {
          control.value = '';
        }
        current[key] = BY_KEY.get(key).type === 'list' ? [] : null;
      }
      onApply(collect());
    },
  }, 'Clear');

  const el = h('form', {
    class: 'toolbar analytics-filter-bar',
    role: 'search',
    'aria-label': 'Report filters',
    onSubmit: (ev) => { ev.preventDefault(); onApply(collect()); },
  }, [
    ...rendered,
    h('div', { class: 'field field-action analytics-filter-actions' }, [applyBtn, clearBtn]),
  ]);

  return { el, collect, controls };
}

/**
 * The chips that state, above the data, exactly which dimensions are narrowing
 * it — and, when a source could not honour one, which were DROPPED.
 *
 * The dropped set is the honest half. A ledger fallback narrows by entity,
 * plant and project; a reader who set a vendor filter and sees a figure has
 * been shown an answer to a question they did not ask unless the screen says
 * so here.
 *
 * @param {Object} filters
 * @param {string[]} [unapplied] - FilterSet keys the answering source ignored.
 */
export function filterChips(filters, unapplied = []) {
  const active = activeFilters(filters);
  const dropped = new Set(unapplied || []);
  const wrap = h('div', { class: 'analytics-chips', 'data-active': String(active.length) });
  if (!active.length) {
    wrap.appendChild(h('span', { class: 'xs muted' },
      'No filter is applied — this is the whole of the portfolio your access scope reaches.'));
    return wrap;
  }
  wrap.appendChild(h('span', { class: 'xs muted' }, 'Filtered by:'));
  for (const item of active) {
    const isDropped = dropped.has(item.key);
    wrap.appendChild(h('span', {
      class: `analytics-chip${isDropped ? ' analytics-chip-dropped' : ''}`,
      'data-filter': item.key,
      title: isDropped
        ? `The source that answered this screen does not support a ${item.label.toLowerCase()} `
          + 'filter, so this dimension was NOT applied to the figures shown.'
        : `${item.label}: ${item.value}`,
    }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, isDropped ? '!' : '·'),
      text(` ${item.label}: ${item.value}`),
      isDropped ? h('span', { class: 'sr-only' }, ' — not applied by the answering source') : null,
    ].filter(Boolean)));
  }
  return wrap;
}
