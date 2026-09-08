/* app/frontend/src/features/analytics/analytics-shapes.js
   One reading of two response shapes, so eleven screens do not each guess.

   THE PROBLEM
   -----------
   `/api/dashboard` answers `{ projects, totals, alerts }`. A1's
   `/api/reports/{report_id}` will answer `{ rows, totals, meta }`. Both carry
   the SAME ten metrics, because both derive from `domain.compute_ledger` —
   the frozen `C5_formulas.json` registry in code — and the Wave 7 contract is
   explicit that they must: "Do not re-derive them in SQL from memory. The
   reviewer has already found two functions in one file computing `open_paise`
   differently."

   If each screen did its own `data.rows || data.projects || data.items`, the
   eleven would drift, and the drift would be invisible: a screen reading the
   wrong key renders an empty table, which looks exactly like a screen with no
   data. So the reading happens once, here, and a shape neither branch
   recognises is reported as UNREADABLE rather than silently treated as empty.

   THE TEN METRICS ARE NEVER RECOMPUTED
   ------------------------------------
   `exposure` and `available` are derived quantities — `commitment + actual +
   pr_reserved` and `budget − exposure` — and this module does NOT compute
   them even though it easily could. It reads what the server sent. A second
   implementation of a frozen formula is a second thing to keep in step with
   the registry, and the one that drifts is always the copy.

   The single exception is documented where it happens: `utilisationBp()`
   computes basis points from `exposure` and `budget` IN INTEGERS, because the
   server's own `utilisation_pct` is a float rounded to one decimal place and a
   float is not something to draw a breach threshold from. The underlying
   integers are the authority; the float is a display convenience this feature
   does not use.
*/

import { basisPoints } from './analytics-metrics.js';

/** The ten metrics, in the order the Wave 7 contract lists them. */
export const METRICS = Object.freeze([
  'budget', 'original', 'revisions', 'ordered', 'commitment',
  'actual', 'received', 'received_not_billed', 'pr_reserved', 'available',
]);

/**
 * What each metric MEANS, in one sentence, rendered as the card's sub-line and
 * as a column title.
 *
 * These are not decoration. `open commitment is ordered less BILLED, never
 * ordered less received` is the arithmetic the whole CAPEX control exists to
 * protect, and a reader who cannot see which of the four buckets a column is
 * cannot tell whether two of them have been added together.
 */
export const METRIC_DEFINITION = Object.freeze({
  budget: 'The current approved budget: the original plus every approved revision.',
  original: 'The budget as first approved, before any revision.',
  revisions: 'The net effect of every approved budget revision and transfer.',
  ordered: 'The gross value of approved purchase orders. Not a commitment on its own.',
  commitment: 'Open PO commitment: ordered less BILLED, floored at zero, and zero once the '
    + 'order is released. A receipt does not release a commitment; a bill does.',
  actual: 'Actual CWIP: the value of posted vendor bills.',
  received: 'The value received against purchase orders, whether or not it has been billed.',
  received_not_billed: 'Received and not yet invoiced. Separate exposure — it overlaps open '
    + 'commitment by design and the two are never added together.',
  pr_reserved: 'Value reserved by approved purchase requests that have not yet become orders.',
  exposure: 'Open commitment plus actual CWIP plus PR reservations: what this can still cost '
    + 'plus what it already has.',
  available: 'Budget less exposure. What remains commitable.',
});

/** Short column headings for the same ten. */
export const METRIC_LABEL = Object.freeze({
  budget: 'Current budget',
  original: 'Original budget',
  revisions: 'Revisions',
  ordered: 'Ordered',
  commitment: 'Open commitment',
  actual: 'Actual CWIP',
  received: 'Received',
  received_not_billed: 'Received, not billed',
  pr_reserved: 'PR reserved',
  exposure: 'Exposure',
  available: 'Available',
});

/**
 * The identity fields each reportable dimension supplies, when a grouped row
 * is read into the vocabulary `projectRow()` and `flattenWbs()` already speak.
 *
 * `reporting.DIMENSIONS` selects a KEY and a LABEL per dimension — for
 * `project`, `f.project_id` and `pr.capex_code` — and returns them in two
 * side-maps rather than on the row. Both halves are carried across: dropping
 * the key would leave a screen unable to build a drill-down link, and dropping
 * the label would leave it rendering raw ids at a controller.
 *
 * `category` and `budget_head` deliberately land on the SAME pair of fields,
 * because AMB-04 reads them as one column and the backend narrows both to
 * `budget_head_ids`. Two names for one fact, kept as one fact.
 */
const DIMENSION_FIELDS = Object.freeze({
  entity: { key: 'entity_id', label: 'entity' },
  plant: { key: 'plant_id', label: 'plant' },
  location: { key: 'location_id', label: 'location' },
  project: { key: 'project_id', label: 'capex_code' },
  wbs: { key: 'wbs_id', label: 'wbs_code' },
  budget_head: { key: 'budget_head_id', label: 'budget_head' },
  category: { key: 'budget_head_id', label: 'budget_head' },
});

/**
 * Read ONE grouped row from `/api/reports/metrics` into the flat vocabulary.
 *
 * The metrics are already flat on the row and integer paise, so they are
 * spread across UNTOUCHED — not recomputed, not rescaled, not defaulted. Only
 * the `key` and `labels` side-maps are lifted onto named fields.
 *
 * `raw` keeps the original, and `group_key` keeps the whole key map, so a
 * drill-down can send back exactly the key the server grouped on rather than a
 * value re-derived from a label that may not be unique.
 */
export function adaptGroupedRow(raw) {
  const r = raw || {};
  const key = (r.key && typeof r.key === 'object') ? r.key : {};
  const labels = (r.labels && typeof r.labels === 'object') ? r.labels : {};
  const out = { ...r, group_key: key, group_labels: labels };
  for (const [dimension, field] of Object.entries(DIMENSION_FIELDS)) {
    if (Object.prototype.hasOwnProperty.call(key, dimension) && key[dimension] !== undefined) {
      out[field.key] = key[dimension];
    }
    if (labels[dimension] !== undefined && labels[dimension] !== null) {
      out[field.label] = labels[dimension];
    }
  }
  /* A grouped row's display name is its label, and a row grouped on something
     with no label (an id the join found no row for) keeps its id rather than
     acquiring a fabricated name. `name` is left absent when neither exists —
     `projectRow()` renders an absent name as a dash, at the cell, where the
     reason can be attached. */
  if (out.name === undefined) {
    const first = (r.group_by && r.group_by[0]) || Object.keys(labels)[0] || Object.keys(key)[0];
    const field = first ? DIMENSION_FIELDS[first] : null;
    if (field && out[field.label] !== undefined) out.name = out[field.label];
  }
  return out;
}

/**
 * A whole `/api/reports/metrics` (or `/drill-down`) payload, read into the
 * shape the eleven screens already consume.
 *
 * `rows`, `totals`, `state`, `next_cursor`, `has_more` and `freshness` are
 * PASSED THROUGH, because every one of them is the server's own answer and
 * this function's job is translation, not arithmetic. Not one number is
 * touched: the totals are the server's totals over the whole filtered
 * population, and re-deriving them from the page would make them the totals of
 * whatever happened to be on screen.
 */
export function adaptReportPayload(data) {
  if (!data || typeof data !== 'object' || !Array.isArray(data.rows)) return data;
  const groupBy = Array.isArray(data.group_by) ? data.group_by : [];
  return {
    ...data,
    rows: data.rows.map((row) => adaptGroupedRow({ group_by: groupBy, ...row })),
  };
}

/**
 * Pull the row array out of whichever shape answered.
 *
 * @returns {{rows: Array, readable: boolean, key: string|null}}
 *   `readable: false` means NO recognised key was present — which a screen
 *   renders as a failure, not as an empty result. A response this module
 *   cannot read is a contract mismatch between the client and the server, and
 *   showing "nothing matched" for it would hide a real integration defect
 *   behind a reassuring sentence.
 */
export function readRows(data, extraKeys = []) {
  if (Array.isArray(data)) return { rows: data, readable: true, key: '(top level array)' };
  if (!data || typeof data !== 'object') {
    return { rows: [], readable: false, key: null };
  }
  for (const key of ['rows', 'items', 'projects', ...extraKeys]) {
    if (Array.isArray(data[key])) return { rows: data[key], readable: true, key };
  }
  return { rows: [], readable: false, key: null };
}

/**
 * Pull the server's own totals block out of whichever shape answered.
 *
 * Returns null when the source sent none — which a screen renders as "not
 * reported" on every card rather than summing the rows itself. Summing the
 * rows would produce the total OF THE ROWS ON SCREEN, which on any paginated
 * source is the total of one page presented as the total of a portfolio.
 */
export function readTotals(data) {
  if (!data || typeof data !== 'object') return null;
  const totals = data.totals || data.summary || data.total || null;
  return totals && typeof totals === 'object' ? totals : null;
}

/**
 * One metric off a totals or row object, as integer paise or null.
 *
 * The key aliases exist because the ledger names a column `actual` and a
 * reporting service may name the same quantity `actual_paise`. Both are the
 * same number; neither is converted, scaled or defaulted. An absent metric is
 * `null` — never 0 — because zero is a measurement and "the source did not
 * report this" is not.
 */
export function metric(obj, name) {
  if (!obj || typeof obj !== 'object') return null;
  for (const key of [name, `${name}_paise`, `${name}Paise`]) {
    const v = obj[key];
    if (v !== null && v !== undefined) return v;
  }
  return null;
}

/** Exposure, read rather than derived; null when the source did not send it. */
export function exposureOf(obj) {
  return metric(obj, 'exposure');
}

/**
 * Utilisation in integer BASIS POINTS, from exposure and budget.
 *
 * The one derivation in this file, and it is derived rather than read on
 * purpose: the server's `utilisation_pct` is a float rounded to one decimal,
 * and a band that decides "breach" from a rounded float will call 99.96%
 * "100.0%" and change a screen's colour on a rounding artefact. The integers
 * it was rounded from are right there; basisPoints() divides them once, in
 * integers, and truncates — so a bar that has not reached the line never draws
 * as though it had.
 *
 * @returns {number|null} null when either input is absent, or the budget is
 *   zero. A row with no budget has no utilisation: that is not 0% and not 100%.
 */
export function utilisationBp(obj) {
  const exposure = exposureOf(obj);
  const budget = metric(obj, 'budget');
  if (exposure === null || budget === null) return null;
  return basisPoints(exposure, budget);
}

/** Actual CWIP as a share of budget, in basis points. */
export function actualBp(obj) {
  const actual = metric(obj, 'actual');
  const budget = metric(obj, 'budget');
  if (actual === null || budget === null) return null;
  return basisPoints(actual, budget);
}

/**
 * A project row, read from either shape into one vocabulary.
 *
 * Identity fields keep their `null` when absent rather than becoming '—' here:
 * the dash is a RENDERING decision and belongs at the cell, where the reason
 * for it can be attached to a title. A model that pre-dashes its own fields
 * cannot tell a screen the difference between "no vendor" and "vendor not
 * returned by this source".
 */
export function projectRow(raw) {
  const r = raw || {};
  const out = {
    project_id: r.project_id ?? r.id ?? null,
    capex_code: r.capex_code ?? r.code ?? null,
    name: r.name ?? r.project_name ?? null,
    entity: r.entity ?? r.entity_name ?? null,
    plant: r.plant ?? r.plant_name ?? null,
    location: r.location ?? r.location_name ?? null,
    status: r.status ?? null,
    band: r.band ?? null,
    raw: r,
  };
  for (const key of [...METRICS, 'exposure']) out[key] = metric(r, key);
  out.utilisation_bp = utilisationBp(r);
  out.actual_bp = actualBp(r);
  return out;
}

/**
 * A WBS node, read into one vocabulary and FLATTENED with its depth.
 *
 * The tree arrives nested; a tree TABLE needs a flat list that remembers how
 * deep each row was. Flattening is iterative and depth-bounded for the same
 * reason `main.py::wbs_tree` serialises iteratively: a recursive walk over a
 * deep hierarchy is a RecursionError, and that one has already been fixed once
 * on the server side.
 *
 * `carries_budget` is preserved because it is load-bearing: a node that does
 * not carry budget shows its ROLLUP, and reading a rollup as an own-value
 * double counts every ancestor.
 */
export function flattenWbs(tree, { maxDepth = 200 } = {}) {
  const out = [];
  const stack = [];
  for (let i = (tree || []).length - 1; i >= 0; i -= 1) stack.push({ node: tree[i], depth: 0 });
  let truncated = false;
  while (stack.length) {
    const { node, depth } = stack.pop();
    if (!node) continue;
    if (depth >= maxDepth) { truncated = true; continue; }
    const totals = node.total || node.own || node;
    const row = {
      wbs_id: node.wbs_id ?? null,
      wbs_code: node.wbs_code ?? null,
      description: node.description ?? null,
      level: node.level ?? depth + 1,
      depth,
      status: node.status ?? null,
      progress_pct: node.progress_pct ?? null,
      budget_head: node.budget_head ?? null,
      responsible_user: node.responsible_user ?? null,
      asset_category: node.asset_category ?? null,
      planned_start: node.planned_start ?? null,
      planned_end: node.planned_end ?? null,
      carries_budget: node.carries_budget === true,
      budget_owner_code: node.budget_owner_code ?? null,
      allow_procurement: node.allow_procurement !== false,
      allow_posting: node.allow_posting !== false,
      is_abandoned: node.is_abandoned === true,
      has_children: Array.isArray(node.children) && node.children.length > 0,
      own: node.own || null,
      raw: node,
    };
    for (const key of [...METRICS, 'exposure']) row[key] = metric(totals, key);
    row.utilisation_bp = utilisationBp(totals);
    row.actual_bp = actualBp(totals);
    out.push(row);
    const children = Array.isArray(node.children) ? node.children : [];
    for (let i = children.length - 1; i >= 0; i -= 1) {
      stack.push({ node: children[i], depth: depth + 1 });
    }
  }
  return { rows: out, truncated };
}

/**
 * The alert blocks `/api/dashboard` returns, read into one shape.
 *
 * Every list is kept SEPARATE and none is concatenated into a single "issues"
 * count. A budget exception, an over-billed line and an unbilled receipt are
 * three different problems with three different owners, and a single number
 * covering all three is a number nobody can act on.
 */
export function readAlerts(data) {
  const a = (data && typeof data === 'object' && data.alerts) || {};
  const list = (key) => (Array.isArray(a[key]) ? a[key] : []);
  return {
    budget_exceptions: list('budget_exceptions'),
    over_billed_lines: list('over_billed_lines'),
    received_not_billed_lines: list('received_not_billed_lines'),
    pending_revisions: list('pending_revisions'),
    awaiting_capitalisation: list('awaiting_capitalisation'),
    reconciliation_exceptions: list('reconciliation_exceptions'),
    present: !!(data && typeof data === 'object' && data.alerts),
  };
}
