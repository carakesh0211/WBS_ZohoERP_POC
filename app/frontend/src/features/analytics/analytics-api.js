/* app/frontend/src/features/analytics/analytics-api.js
   The contract surface for Wave 7's eleven reporting screens.

   WHAT THIS MODULE IS FOR
   -----------------------
   `app/backend/pg/reporting.py` + `api/reports.py` (agent A1) and
   `api/exports.py` (agent A2) are being written WHILE these screens are being
   written. Neither is guaranteed to exist in the tree this file is loaded
   from. That is not a reason to stub a number, and it is not a reason to
   block: this codebase already has the pattern for exactly this situation and
   this module follows it rather than inventing a second one.

   `features/integration/integration-api.js` established it in Wave 5:

     * presence is PROBED from the application's own /openapi.json, not
       guessed, so an unmounted route renders as ABSENT rather than as an empty
       result — "no CWIP rows matched" and "this build has no CWIP report" are
       different sentences and only one of them is true today;
     * a 503 carrying `unavailable: true` means MOUNTED-BUT-CANNOT-ANSWER and
       falls through to the next source exactly as an absent route does, while
       a BARE 503 stays a real fault and renders as one;
     * every call returns `{ data, source, template }` and no screen renders
       data without rendering its source.

   The probe, `available()`, `classifyMissing()` and `classifyUnavailable()`
   are IMPORTED from that module, not copied. A second copy of the 503 rule is
   how the two would come to disagree about what "unavailable" means, and the
   whole value of the rule is that one answer holds everywhere.

   THE THREE SOURCES, AND WHAT EACH ONE CAN HONESTLY ANSWER
   --------------------------------------------------------
   'reports'  — A1's `/api/reports/{report_id}`. The only source that applies
                the canonical FilterSet server-side, groups by the requested
                dimensions, and returns totals it computed itself over the
                caller's whole resolved scope.

   'ledger'   — the routes this build mounts TODAY: `/api/dashboard`,
                `/api/projects/{project_id}/wbs`, `/api/reconciliation`,
                `/api/purchase-orders`, `/api/bills`, `/api/grns`,
                `/api/capitalisation`. These are real, server-computed figures
                from `domain.compute_ledger` — the same frozen C5 registry A1
                will derive from — so a total taken from here is a MEASURED
                total, not an approximation. What they cannot do is honour the
                full FilterSet: they accept `entity_id`, `plant_id` and
                `project_id` and nothing else. A screen reading this source
                therefore names the filters that were NOT applied, because a
                figure narrowed by three of nine filters presented as though it
                had been narrowed by nine is a wrong number wearing a right
                number's clothes.

   'unavailable' — neither. The screen renders what is missing, by route name,
                and shows no figure at all.

   WHAT THIS MODULE WILL NOT DO
   ----------------------------
   It never computes a total. Not a sum, not a ratio, not a bucket. Every
   figure any screen renders is a figure a server sent. `assertSumsBack()` in
   analytics-metrics.js CHECKS the server's arithmetic against the rows the
   server returned, and reports a discrepancy rather than papering over it —
   that is the opposite operation and it is the only arithmetic in this
   feature.
*/

import { createApiClient, ApiClientError } from '../../core/api-client.js';
import {
  available,
  classifyMissing,
  classifyUnavailable,
  unavailableNoteFrom,
  EndpointUnavailableError,
} from '../integration/integration-api.js';

export { EndpointUnavailableError };

/**
 * The analytics family's error type. `err instanceof AnalyticsApiError`
 * discriminates a reporting failure from an approval, budget, audit, settings
 * or integration failure, while `err instanceof ApiClientError` still catches
 * all of them.
 */
export class AnalyticsApiError extends ApiClientError {
  constructor(message, opts) {
    super(message, opts);
    this.name = 'AnalyticsApiError';
  }
}

/**
 * Raised when the caller's RESOLVED SCOPE is empty for this request — the
 * server answered, and the answer was "you hold no grant that reaches any of
 * this", not "nothing matched".
 *
 * THIS IS NOT AN EXISTENCE ORACLE, AND THE DISTINCTION IS EXACT.
 * `core/api-client.js` collapses a 403 on a GET into `notfound` precisely
 * because a differently worded refusal on an IDENTIFIED record tells the
 * caller that record exists. That rule is untouched here and every by-id read
 * in this feature still goes through it. What this error models is different:
 * a COLLECTION read where the server states, about the caller's own grants,
 * that the resolved scope is empty. It names no record, confirms no id, and
 * withholding it would collapse "you have no grant" into "there is no data" —
 * which is the single most misleading thing a financial control screen can
 * say, because it reads as a clean bill of health.
 *
 * The server declares it; this module never infers it.
 */
export class ScopeDeniedError extends Error {
  /**
   * @param {string} [detail] - the server's own words about which grant is
   *   missing. Rendered verbatim; never invented here.
   */
  constructor(detail) {
    super(detail || 'Your access scope resolves to nothing for this request.');
    this.name = 'ScopeDeniedError';
    this.detail = detail || '';
  }
}

const MESSAGES = {
  network: 'The reporting service could not be reached. Check your connection and try again.',
  auth: 'Your session has ended. Sign in again to continue.',
  notfound: 'No records were found for these filters.',
  forbidden: 'You do not have permission to do this.',
  conflict: 'This report changed since it was loaded. Reload it before continuing.',
  validation: 'These filters could not be accepted. Correct the highlighted fields and try again.',
  error: (status) => `The reporting service returned an unexpected error (HTTP ${status}).`,
  unreadable: 'The reporting service returned a response that could not be understood.',
};

/* Two clients over the ONE shared implementation. Separate base paths so a
   stack trace can say which surface answered; neither adds transport
   behaviour of its own — the X-Session header, the fresh correlation id, the
   RFC-7807 parsing and the not-found-over-forbidden rule all live once, in
   core/api-client.js. */
const reports = createApiClient({
  basePath: '/api/reports', ErrorClass: AnalyticsApiError, messages: MESSAGES,
});
const ledger = createApiClient({
  basePath: '/api', ErrorClass: AnalyticsApiError, messages: MESSAGES,
});

/* ------------------------------------------------------------------ *
 * Route templates — declared, so a screen can name what is missing
 * ------------------------------------------------------------------ */

/**
 * A1's reporting surface, exactly as C14_traceability.json declares it for
 * REQ-RPT-009 and REQ-RPT-010. These are OpenAPI path TEMPLATES, matched
 * against /openapi.json character for character — `{report_id}` is the
 * parameter name FastAPI publishes, not a placeholder this file substitutes.
 */
export const REPORT_ROUTES = Object.freeze({
  report: '/api/reports/{report_id}',
  savedViews: '/api/reports/saved-views',
  export: '/api/reports/export',
});

/** A2's export-job surface. Long exports answer 202 with a job id. */
export const EXPORT_ROUTES = Object.freeze({
  create: '/api/exports',
  job: '/api/exports/{job_id}',
});

/**
 * The report ids these eleven screens ask for.
 *
 * Named here rather than inline so that the UNAVAILABLE state can say
 * `/api/reports/cwip-ageing` — the concrete thing an operator would look for —
 * while the presence probe still checks the template `/api/reports/{report_id}`
 * that FastAPI actually publishes.
 */
export const REPORT_IDS = Object.freeze({
  portfolio: 'portfolio-summary',
  controller: 'controller-workbench',
  projectList: 'project-list',
  projectDetail: 'project-detail',
  wbsHierarchy: 'wbs-hierarchy',
  wbsElement: 'wbs-element',
  cwipLedger: 'cwip-ledger',
  commitmentAgeing: 'open-commitment-ageing',
  cwipAgeing: 'cwip-ageing',
  exceptions: 'exception-overrun',
});

/** The ledger routes that exist in this build today, as templates. */
export const LEDGER_ROUTES = Object.freeze({
  dashboard: '/api/dashboard',
  wbs: '/api/projects/{project_id}/wbs',
  budgetGrid: '/api/projects/{project_id}/budget-grid',
  reconciliation: '/api/reconciliation',
  reconciliationExceptions: '/api/reconciliation/exceptions',
  purchaseOrders: '/api/purchase-orders',
  bills: '/api/bills',
  grns: '/api/grns',
  capitalisation: '/api/capitalisation',
  budgetRevisions: '/api/budget-revisions',
});

/**
 * The FilterSet dimensions the LEDGER routes can actually honour.
 *
 * Everything else in the canonical FilterSet is dropped when a screen falls
 * back, and every screen that falls back says which — see
 * `unappliedFilters()`. This constant is the single place that knowledge
 * lives, so a route that later grows a filter is updated once.
 */
export const LEDGER_HONOURS = Object.freeze(['entity_ids', 'plant_ids', 'project_ids']);

const A1_NOTE = 'Wave 7 agent A1 is building the reporting endpoint this screen reads '
  + '(app/backend/pg/reporting.py and api/reports.py). Until it is mounted there is no source '
  + 'for this figure, so none is shown — an empty table here would read as "nothing matched", '
  + 'which is a claim about your data rather than about this build.';

const A2_NOTE = 'Wave 7 agent A2 is building the export-job endpoint (app/backend/api/exports.py). '
  + 'Until it is mounted this screen cannot queue an export, and offering a button that '
  + 'silently did nothing would be worse than saying so.';

/* ------------------------------------------------------------------ *
 * Scope refusal, declared by the server
 * ------------------------------------------------------------------ */

/**
 * Did the server DECLARE the caller's resolved scope empty?
 *
 * Only an explicit declaration counts. A 403 is NOT enough on its own —
 * core/api-client.js has already collapsed a GET 403 into `notfound` by the
 * time an error reaches here, deliberately, and un-collapsing it in this
 * module would re-open the existence oracle that rule closed.
 *
 * The declaration is a body flag on a collection response or on a refusal:
 * `{"detail": {"scope_denied": true, "missing_grant": "…"}}`. A server that
 * does not send it gets the ordinary rendering, never a guessed one.
 */
export function classifyScopeDenied(err) {
  const body = err && err.body;
  const detail = body && typeof body === 'object' ? body.detail : null;
  if (detail && typeof detail === 'object' && detail.scope_denied === true) {
    return String(detail.missing_grant || detail.message || '') || true;
  }
  return false;
}

/**
 * A SUCCESSFUL response that declares the scope empty.
 *
 * The better shape, and the one A1 should prefer: 200 with zero rows plus
 * `scope_denied: true`, so the client never has to distinguish a refusal from
 * a miss by status code at all.
 */
export function payloadScopeDenied(data) {
  if (!data || typeof data !== 'object') return false;
  if (data.scope_denied === true) return String(data.missing_grant || '') || true;
  const meta = data.meta;
  if (meta && typeof meta === 'object' && meta.scope_denied === true) {
    return String(meta.missing_grant || '') || true;
  }
  return false;
}

/* ------------------------------------------------------------------ *
 * Freshness
 * ------------------------------------------------------------------ */

/**
 * Pull the provenance block out of a response, in whatever shape the source
 * sent it, and NEVER invent one.
 *
 * `ui-contract.md`: "Every screen showing synced data shows its last-sync time
 * and source label. A number with no provenance is not shown." A source that
 * reports no freshness gets `{ known: false }` and the screen renders "not
 * reported" — which is a true statement — rather than `new Date()`, which
 * would be a lie that always looks reassuring.
 *
 * @returns {{known: boolean, asOf: string|null, lastSyncAt: string|null,
 *            stale: boolean, staleReason: string}}
 */
export function freshnessOf(data) {
  const meta = (data && typeof data === 'object' && (data.meta || data.freshness)) || data;
  if (!meta || typeof meta !== 'object') {
    return { known: false, asOf: null, lastSyncAt: null, stale: false, staleReason: '' };
  }
  const asOf = meta.as_of || meta.generated_at || meta.computed_at || null;
  const lastSyncAt = meta.last_sync_at || meta.last_synced_at || meta.synced_at || null;
  const stale = meta.stale === true;
  return {
    known: !!(asOf || lastSyncAt),
    asOf: asOf || null,
    lastSyncAt: lastSyncAt || null,
    stale,
    staleReason: String(meta.stale_reason || meta.staleness_reason || ''),
  };
}

/* ------------------------------------------------------------------ *
 * The source resolver
 * ------------------------------------------------------------------ */

/**
 * Run the first candidate whose route this build mounts.
 *
 * Deliberately the same shape as `integration-api.js::firstAvailable`, because
 * it is the same problem and a screen author moving between the two features
 * should not have to learn a second set of rules. The behaviours that matter:
 *
 *   * `available(template) === false` — skip without calling.
 *   * `available(template) === null` (the schema could not be read) — CALL,
 *     and treat a FastAPI-shaped code-less 404 as absence after all. Unknown
 *     is never coerced to present or to absent.
 *   * a 503 carrying `unavailable: true` — MOUNTED and declaring it cannot
 *     answer. Fall through, and keep the server's own note: it knows which
 *     half is missing and this module does not.
 *   * a BARE 503 — a real fault. It is thrown, and the screen renders a
 *     failure. Treating every 503 as "not built yet" would give a broken
 *     reporting service a permanently calm face.
 *   * a declared scope refusal — thrown as `ScopeDeniedError` and NOT fallen
 *     through. Falling through would answer a scope question from a source
 *     that applies a different scope, which is precisely how a screen comes to
 *     show a number the caller was not entitled to.
 *
 * @param {Array<{source:string, template:string, label:string, call:Function}>} candidates
 * @param {string} unavailableNote
 * @returns {Promise<{data:*, source:string, template:string, label:string,
 *                    freshness:Object, unapplied:string[]}>}
 */
export async function firstAvailable(candidates, unavailableNote) {
  let lastAbsent = null;
  let declaredNote = '';
  for (const candidate of candidates) {
    const present = await available(candidate.template);
    if (present === false) { lastAbsent = candidate; continue; }
    let raw;
    try {
      raw = await candidate.call();
    } catch (err) {
      const denied = classifyScopeDenied(err);
      if (denied) throw new ScopeDeniedError(typeof denied === 'string' ? denied : '');
      if (classifyUnavailable(err)) {
        lastAbsent = candidate;
        declaredNote = unavailableNoteFrom(err) || declaredNote;
        continue;
      }
      if (present === null && classifyMissing(err)) { lastAbsent = candidate; continue; }
      throw err;
    }
    const denied = payloadScopeDenied(raw);
    if (denied) throw new ScopeDeniedError(typeof denied === 'string' ? denied : '');
    return {
      data: raw,
      source: candidate.source,
      template: candidate.template,
      label: candidate.label || candidate.template,
      freshness: freshnessOf(raw),
      unapplied: candidate.unapplied || [],
    };
  }
  throw new EndpointUnavailableError(
    (lastAbsent && lastAbsent.template) || candidates[0].template,
    declaredNote || unavailableNote,
  );
}

/**
 * The FilterSet keys a candidate could NOT honour.
 *
 * A ledger fallback narrows by entity, plant and project and by nothing else.
 * A screen that renders its figure without saying so is presenting a
 * three-dimension answer to a nine-dimension question.
 *
 * @param {Object} filters - a FilterSet (analytics-filters.js).
 * @param {string[]} honoured - keys this source applies.
 * @returns {string[]} the ACTIVE filter keys that were dropped.
 */
export function unappliedFilters(filters, honoured) {
  const out = [];
  for (const [key, value] of Object.entries(filters || {})) {
    if (honoured.includes(key)) continue;
    if (value === null || value === undefined || value === '') continue;
    if (Array.isArray(value) && value.length === 0) continue;
    out.push(key);
  }
  return out;
}

/* Ledger query params, from the canonical FilterSet. Only the three the
   ledger routes actually read; everything else is reported as unapplied
   rather than silently appended to a URL that would ignore it. */
function ledgerParams(filters) {
  const f = filters || {};
  const first = (list) => (Array.isArray(list) && list.length ? list[0] : undefined);
  return {
    entity_id: first(f.entity_ids),
    plant_id: first(f.plant_ids),
    project_id: first(f.project_ids),
  };
}

/**
 * A reporting-surface candidate for one report id.
 *
 * The whole FilterSet is sent as the query. A1 owns the parse; this module
 * owns only that the SAME object goes to cards, charts, tables and exports —
 * which is the requirement a second filter shape would break.
 */
function reportCandidate(reportId, filters, extra = {}) {
  return {
    source: 'reports',
    template: REPORT_ROUTES.report,
    label: `/api/reports/${reportId}`,
    call: () => reports.get(`/${reportId}`, { ...toQuery(filters), ...extra }),
    unapplied: [],
  };
}

/** Serialise a FilterSet into query parameters. Arrays go as repeated keys. */
export function toQuery(filters) {
  const out = {};
  for (const [key, value] of Object.entries(filters || {})) {
    if (value === null || value === undefined || value === '') continue;
    if (Array.isArray(value)) {
      if (!value.length) continue;
      out[key] = value;
      continue;
    }
    out[key] = value;
  }
  return out;
}

/* ------------------------------------------------------------------ *
 * The calls, one per screen
 * ------------------------------------------------------------------ */

/** SCR-01 / SCR-02 / SCR-04: the portfolio, as cards plus one row per project. */
export function getPortfolio(filters, reportId = REPORT_IDS.portfolio) {
  return firstAvailable([
    reportCandidate(reportId, filters),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.dashboard,
      label: '/api/dashboard',
      call: () => ledger.get('/dashboard', ledgerParams(filters)),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS),
    },
  ], A1_NOTE);
}

/** SCR-05 / SCR-06 / SCR-07 / SCR-08: one project's WBS, with its ledger. */
export function getWbs(projectId, filters, reportId = REPORT_IDS.wbsHierarchy) {
  if (!projectId) {
    return Promise.reject(new AnalyticsApiError(
      'Choose a CAPEX project to load its WBS hierarchy.',
      { status: 0, kind: 'validation' },
    ));
  }
  return firstAvailable([
    reportCandidate(reportId, { ...filters, project_ids: [projectId] }),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.wbs,
      label: `/api/projects/${projectId}/wbs`,
      call: () => ledger.get(`/projects/${encodeURIComponent(projectId)}/wbs`),
      unapplied: unappliedFilters(filters, ['project_ids']),
    },
  ], A1_NOTE);
}

/** SCR-19: the CWIP ledger — posted vendor bills against WBS. */
export function getCwipLedger(filters) {
  return firstAvailable([
    reportCandidate(REPORT_IDS.cwipLedger, filters),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.bills,
      label: '/api/bills',
      call: () => ledger.get('/bills', ledgerParams(filters)),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS),
    },
  ], A1_NOTE);
}

/**
 * SCR-23 / SCR-24: an ageing distribution.
 *
 * THERE IS NO LEDGER FALLBACK HERE, AND THAT IS THE POINT.
 * A bucket table is a GROUPED AGGREGATE over the caller's whole resolved
 * scope. `/api/purchase-orders` and `/api/bills` return a page of documents;
 * bucketing that page in the browser would produce a table whose totals are
 * the totals of one page and whose column headings claim to be the totals of a
 * portfolio. That is the exact failure this feature's rules exist to prevent,
 * so the buckets stay UNAVAILABLE until A1 mounts the report.
 *
 * The screens still show the un-bucketed TOTAL, because that one IS measured —
 * it comes from `/api/reconciliation`'s own summary — and they fetch it
 * separately through `getCommitmentTotal()` / `getCwipTotal()`.
 */
export function getAgeing(kind, filters) {
  const reportId = kind === 'cwip' ? REPORT_IDS.cwipAgeing : REPORT_IDS.commitmentAgeing;
  return firstAvailable([reportCandidate(reportId, filters)],
    `${A1_NOTE} An ageing table cannot be assembled in the browser from a page of documents: `
    + 'the buckets would be the buckets of that page while the headings claimed to be the '
    + 'buckets of the portfolio.');
}

/** The measured, un-bucketed open-commitment total — server-summed. */
export function getCommitmentTotal(filters) {
  return firstAvailable([
    reportCandidate(REPORT_IDS.commitmentAgeing, filters, { shape: 'total' }),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.reconciliation,
      label: '/api/reconciliation',
      call: () => ledger.get('/reconciliation', ledgerParams(filters)),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS),
    },
  ], A1_NOTE);
}

/** SCR-25: exceptions and overruns. */
export function getExceptions(filters) {
  return firstAvailable([
    reportCandidate(REPORT_IDS.exceptions, filters),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.dashboard,
      label: '/api/dashboard',
      call: () => ledger.get('/dashboard', ledgerParams(filters)),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS),
    },
  ], A1_NOTE);
}

/** The drill-down behind a metric: the SOURCE RECORDS, same FilterSet. */
export function getDrilldown(reportId, filters, dimension) {
  return firstAvailable([
    reportCandidate(reportId, filters, dimension ? { drill: dimension } : {}),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.dashboard,
      label: '/api/dashboard',
      call: () => ledger.get('/dashboard', ledgerParams(filters)),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS),
    },
  ], A1_NOTE);
}

/**
 * Queue an export of exactly what is on screen.
 *
 * Returns `{ queued: false, reason }` rather than throwing when neither export
 * route is mounted, so a screen can DISABLE the control and say why instead of
 * offering a button that fails when pressed.
 */
export async function queueExport(reportId, filters, format = 'csv') {
  const viaExports = await available(EXPORT_ROUTES.create);
  const viaReports = await available(REPORT_ROUTES.export);
  if (viaExports === false && viaReports === false) {
    return { queued: false, reason: A2_NOTE, template: EXPORT_ROUTES.create };
  }
  const body = { report_id: reportId, format, filters: toQuery(filters) };
  const result = await firstAvailable([
    {
      source: 'exports',
      template: EXPORT_ROUTES.create,
      label: '/api/exports',
      call: () => ledger.post('/exports', body),
    },
    {
      source: 'reports',
      template: REPORT_ROUTES.export,
      label: '/api/reports/export',
      call: () => reports.post('/export', body),
    },
  ], A2_NOTE);
  return { queued: true, ...result };
}

/** Is an export possible in this build at all? Used to gate the control. */
export async function exportAvailable() {
  const a = await available(EXPORT_ROUTES.create);
  const b = await available(REPORT_ROUTES.export);
  if (a === true || b === true) return true;
  if (a === false && b === false) return false;
  return null;   // unknown stays unknown
}

export const NOTES = Object.freeze({ reporting: A1_NOTE, exporting: A2_NOTE });
