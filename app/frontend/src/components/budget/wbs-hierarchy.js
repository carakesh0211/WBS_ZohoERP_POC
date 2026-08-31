/* app/frontend/src/components/budget/wbs-hierarchy.js
   Pure helper: turns the flat list GET /api/budget/cells returns into a
   depth-sorted hierarchy, using wbs_path exactly as
   docs/WAVE2_CONTRACTS.md describes it — the dotted code that "budget" and
   "available" are already subtree sums over (server-computed; this module
   never re-derives a rollup, it only orders and indents what the API sent).

   wbs_path is a dot-delimited code, e.g. "01.02.03" — depth is the segment
   count and the parent path is every segment but the last. No assumption is
   made about it beyond that; a row whose path does not parse as dotted
   segments is still shown, at depth 0, rather than dropped.
*/

/** @param {string} path */
export function depthOf(path) {
  const s = String(path || '').trim();
  if (!s) return 0;
  return s.split('.').length - 1;
}

/** @param {string} path */
export function parentPathOf(path) {
  const s = String(path || '').trim();
  const i = s.lastIndexOf('.');
  return i === -1 ? null : s.slice(0, i);
}

/** Natural compare of dotted numeric-ish segments, so "2" sorts before "10". */
export function compareWbsPath(a, b) {
  const as = String(a || '').split('.');
  const bs = String(b || '').split('.');
  const len = Math.max(as.length, bs.length);
  for (let i = 0; i < len; i += 1) {
    const av = as[i] ?? '';
    const bv = bs[i] ?? '';
    const an = /^\d+$/.test(av) ? Number(av) : null;
    const bn = /^\d+$/.test(bv) ? Number(bv) : null;
    if (an !== null && bn !== null && an !== bn) return an - bn;
    if (av !== bv) return av < bv ? -1 : 1;
  }
  return 0;
}

/**
 * Sort a flat list of rows (each carrying `wbs_path`) into hierarchy order —
 * every node's ancestors appear before it, siblings in path order — and
 * annotate each with `_depth` and `_parentPath`. Rows sharing the same
 * wbs_path (e.g. one per budget head) keep their relative order and all
 * receive the same depth.
 * @param {Array<Object>} rows
 * @returns {Array<Object>} the same row objects, resorted, each with
 *   `_depth` (number) and `_parentPath` (string|null) added.
 */
export function sortHierarchy(rows) {
  const withMeta = (rows || []).map((row, i) => ({
    row,
    i,
    depth: depthOf(row.wbs_path),
    parentPath: parentPathOf(row.wbs_path),
  }));
  withMeta.sort((a, b) => {
    const c = compareWbsPath(a.row.wbs_path, b.row.wbs_path);
    if (c !== 0) return c;
    return a.i - b.i; // stable for same-path rows (different budget heads)
  });
  for (const m of withMeta) {
    m.row._depth = m.depth;
    m.row._parentPath = m.parentPath;
  }
  return withMeta.map((m) => m.row);
}

/**
 * Distinct wbs_path values that are an ancestor of some other row present in
 * `rows` — i.e. the set of paths that can legitimately show a tree-toggle.
 * @param {Array<Object>} rows
 * @returns {Set<string>}
 */
export function pathsWithChildren(rows) {
  const present = new Set((rows || []).map((r) => String(r.wbs_path || '')));
  const withKids = new Set();
  for (const p of present) {
    const parent = parentPathOf(p);
    if (parent !== null && present.has(parent)) withKids.add(parent);
  }
  return withKids;
}
