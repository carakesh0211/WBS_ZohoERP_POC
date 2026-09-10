/* app/frontend/src/features/analytics/analytics-api.js
   The contract surface for Wave 7's eleven reporting screens.

   WHAT THIS MODULE IS FOR
   -----------------------
   These screens were written BEFORE `api/reports.py` and `api/exports.py`
   existed, so this file guessed their contract. The guess was wrong in every
   particular, and it failed in the most expensive way available: the probe
   found `/api/reports/{report_id}` absent, every screen rendered an honest
   "not available in this build", and the reporting service that answers them
   sat mounted three routes away.

   THE BACKEND CONTRACT IS AUTHORITATIVE. It was built against the frozen
   FilterSet in `docs/WAVE7_CONTRACT.md`; this file is the side that moves.
   What was guessed, and what is actually served:

     guessed                        real
     /api/reports/{report_id}       /api/reports/metrics      (grouped totals)
                                    /api/reports/drill-down   (rows behind one)
                                    /api/reports/dimensions   (what it can do)
                                    /api/reports/freshness    (provenance)
     /api/reports/saved-views       /api/reports/views
     /api/reports/export            /api/exports              (202 + job id)

   There is no `report_id` in the real contract and this module no longer
   invents one. A "report" is a `group_by` over one FilterSet, so what used to
   be eleven report ids is now a per-screen DEFAULT GROUPING plus the frozen
   `report_key` the saved-view table accepts (`reporting.REPORT_KEYS`).

   That is not a reason to stub a number, and it is not a reason to block:
   this codebase already has the pattern for exactly this situation and this
   module follows it rather than inventing a second one.

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
   'reports'  — `/api/reports/metrics` and `/api/reports/drill-down`. The only
                source that applies the canonical FilterSet server-side, groups
                by the requested dimensions, and returns totals it computed
                itself over the caller's whole resolved scope.

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
import { adaptReportPayload } from './analytics-shapes.js';
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

/**
 * The server APPLIED NOTHING because one of the filters cannot be expressed.
 *
 * `reporting.UNSUPPORTED_FILTERS` refuses `vendor_ids` and `item_ids` with a
 * 422 carrying `state: "unavailable"` and a coded reason: there is no item_id
 * on any procurement line, and `purchase_order` carries a vendor NAME while
 * `bill` carries a vendor id, so a vendor filter would narrow the actuals and
 * leave the commitments across the whole estate.
 *
 * THIS IS NOT A VALIDATION ERROR AND MUST NOT RENDER AS ONE. The shared client
 * maps every 422 to `kind: 'validation'`, whose wording is "correct the
 * highlighted fields" — advice the reader cannot take, because nothing they
 * typed is malformed. The filter is well-formed and this build cannot answer
 * it, which is the UNAVAILABLE state, named after the exact field.
 *
 * It is deliberately NOT an `EndpointUnavailableError`: the route is mounted
 * and working. Saying "this deployment does not mount /api/reports/metrics"
 * would be false, and a false explanation is worse than a generic one.
 */
export class FilterUnavailableError extends Error {
  /**
   * @param {string} detail - the server's own reason, rendered verbatim.
   * @param {string} code - e.g. 'VENDOR_DIMENSION_INCOMPLETE'.
   * @param {Array<{field:string, code:string, detail:string}>} fields
   */
  constructor(detail, code, fields) {
    super(detail || 'This build cannot apply one of the filters you set.');
    this.name = 'FilterUnavailableError';
    this.detail = detail || '';
    this.code = code || '';
    this.fields = Array.isArray(fields) ? fields : [];
  }
}

/**
 * Did the server refuse a filter it cannot express?
 *
 * The test is the DECLARED `state`, not the status code. A 422 from FastAPI's
 * own request validation (a malformed date, a limit out of range) really is a
 * validation error and keeps rendering as one; only a body that says
 * `state: "unavailable"` is this.
 */
/**
 * The reporting route is MOUNTED and has declared it cannot answer.
 *
 * `api/reports.py::_get_database` answers 503 with
 * `{"code": "DATABASE_NOT_CONFIGURED", "state": "unavailable", "message": …}`
 * when the router is mounted into a process that has no reporting database —
 * which is the state of any deployment still running on SQLite, including the
 * VRT harness, because `run.py` builds the PostgreSQL runtime only when
 * `CAPEX_DB_URL` or `CAPEX_DB_HOST` is set.
 *
 * This is the seam `integration-api.js::classifyUnavailable` already models —
 * mounted, working, and saying which half is missing — spelled with a `state`
 * rather than an `unavailable` flag. Both are read, here, because the shared
 * classifier tests only for the flag and would let this reach the screen as
 * "the reporting service returned an unexpected error (HTTP 503)": a red fault
 * on a build that is simply not configured for it, and one that would hide the
 * ledger data the screen could have shown.
 *
 * A BARE 503 IS STILL A FAULT. The test is the declaration, not the status: a
 * 503 from a proxy, a cold start or a crashed upstream carries neither flag
 * and keeps rendering as the failure it is. Treating every 503 as "not
 * configured" would give a broken reporting service a permanently calm face.
 */
export function classifyDeclaredUnavailable(err) {
  if (classifyUnavailable(err)) return true;
  if (!err || err.status !== 503) return false;
  const body = err.body;
  const detail = body && typeof body === 'object' ? body.detail : null;
  return !!(detail && typeof detail === 'object' && detail.state === 'unavailable');
}

export function classifyFilterUnavailable(err) {
  if (!err || (err.status !== 422 && err.status !== 400)) return null;
  const body = err.body;
  const detail = body && typeof body === 'object' ? body.detail : null;
  if (!detail || typeof detail !== 'object') return null;
  if (detail.state !== 'unavailable') return null;
  return new FilterUnavailableError(
    detail.detail || err.message, detail.code, detail.unsupported_filters,
  );
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
 * The reporting surface, exactly as `api/reports.py` declares it. These are
 * OpenAPI path TEMPLATES, matched against /openapi.json character for
 * character — `{view_id}` is the parameter name FastAPI publishes, not a
 * placeholder this file substitutes.
 *
 * `/api/reports/{report_id}` IS NOT IN THIS LIST because it is not in the
 * build. It never was: it was this file's guess at a contract that had not
 * been written yet, and probing for it is what made eleven working dashboards
 * declare themselves absent.
 */
export const REPORT_ROUTES = Object.freeze({
  metrics: '/api/reports/metrics',
  drillDown: '/api/reports/drill-down',
  dimensions: '/api/reports/dimensions',
  freshness: '/api/reports/freshness',
  views: '/api/reports/views',
  view: '/api/reports/views/{view_id}',
});

/**
 * The export-job surface. Long exports answer 202 with a job id.
 *
 * `{export_job_id}` — NOT `{job_id}`, which was the other half of the same
 * guess. The template has to match what FastAPI publishes or `available()`
 * reports a mounted route as absent.
 */
export const EXPORT_ROUTES = Object.freeze({
  create: '/api/exports',
  job: '/api/exports/{export_job_id}',
  datasets: '/api/exports/datasets',
});

/**
 * The `report_key` values `reporting.REPORT_KEYS` accepts, verbatim.
 *
 * These are NOT route segments and nothing is fetched by them — the reporting
 * API has no per-report route. They are the allow-list the saved-view table
 * checks, so a screen can save and reload a default view. A key absent from
 * the backend's frozen set is refused there, so it is not restated loosely
 * here: seven keys, the seven `REPORT_KEYS` names.
 *
 * Four of the eleven screens (SCR-05, 06, 07, 08) have no key of their own in
 * that frozen set. They are not given a borrowed one — registering a WBS view
 * under `project_list` would make that screen open on a filter set built for
 * another — so those screens simply do not offer a saved view. See
 * `savedViewsSupported()`.
 */
export const REPORT_KEYS = Object.freeze({
  executive: 'executive_dashboard',
  controller: 'controller_workbench',
  projectList: 'project_list',
  cwipLedger: 'cwip_ledger',
  commitmentAgeing: 'open_commitment_ageing',
  cwipAgeing: 'cwip_ageing',
  exceptions: 'exception_monitor',
});

/**
 * The reportable dimensions `reporting.DIMENSIONS` defines.
 *
 * Restated here ONLY as the default this module falls back to when
 * `/api/reports/dimensions` cannot be read. The live list is authoritative and
 * `getDimensions()` fetches it; this constant exists so that a screen whose
 * probe failed groups by something valid rather than by nothing.
 */
export const GROUPABLE = Object.freeze([
  'entity', 'plant', 'location', 'project', 'wbs', 'budget_head', 'category',
  // FABLE 5.1 / migration 026: the real, independent category master.
  'budget_category',
]);

/**
 * One descriptor per screen: what it groups by, which saved-view key it may
 * use, and which export dataset carries its rows.
 *
 * This replaces the eleven invented report ids. A screen is no longer a route
 * segment — it is a DEFAULT GROUPING over the one FilterSet, which is what the
 * reporting API actually models, and saying so here means a screen cannot
 * quietly ask for a grouping the backend refuses.
 *
 * `groupBy` is a DEFAULT, applied only when the FilterSet carries none of its
 * own. A `?group=entity` in the URL wins — the reader chose it, the drill-down
 * and the export inherit it, and overriding it here would make the filter bar
 * a decoration.
 *
 * `key: null` means the frozen `REPORT_KEYS` set has no entry for this screen,
 * so it offers no saved view rather than borrowing another screen's key.
 *
 * `dataset` is the `/api/exports` dataset whose rows ARE what the screen
 * shows. `budget_ledger_cells` is the control cell every metric on these
 * screens is keyed on; `wbs_elements` is the hierarchy the WBS screens render;
 * `purchase_order_lines` is where an open commitment actually lives.
 */
export const SCREENS = Object.freeze({
  executive: Object.freeze({
    name: 'executive', key: REPORT_KEYS.executive,
    groupBy: Object.freeze(['project']), dataset: 'budget_ledger_cells',
  }),
  controller: Object.freeze({
    name: 'controller', key: REPORT_KEYS.controller,
    groupBy: Object.freeze(['project']), dataset: 'budget_ledger_cells',
  }),
  projectList: Object.freeze({
    name: 'projectList', key: REPORT_KEYS.projectList,
    groupBy: Object.freeze(['project']), dataset: 'budget_ledger_cells',
  }),
  projectDetail: Object.freeze({
    name: 'projectDetail', key: null,
    groupBy: Object.freeze(['wbs']), dataset: 'wbs_elements',
  }),
  wbsHierarchy: Object.freeze({
    name: 'wbsHierarchy', key: null,
    groupBy: Object.freeze(['wbs']), dataset: 'wbs_elements',
  }),
  wbsElement: Object.freeze({
    name: 'wbsElement', key: null,
    groupBy: Object.freeze(['wbs']), dataset: 'wbs_elements',
  }),
  cwipLedger: Object.freeze({
    name: 'cwipLedger', key: REPORT_KEYS.cwipLedger,
    groupBy: Object.freeze(['wbs', 'budget_head']), dataset: 'budget_ledger_cells',
  }),
  commitmentAgeing: Object.freeze({
    name: 'commitmentAgeing', key: REPORT_KEYS.commitmentAgeing,
    groupBy: Object.freeze(['project']), dataset: 'purchase_order_lines',
  }),
  cwipAgeing: Object.freeze({
    name: 'cwipAgeing', key: REPORT_KEYS.cwipAgeing,
    groupBy: Object.freeze(['project']), dataset: 'budget_ledger_cells',
  }),
  exceptions: Object.freeze({
    name: 'exceptions', key: REPORT_KEYS.exceptions,
    groupBy: Object.freeze(['project']), dataset: 'budget_ledger_cells',
  }),
});

/** Does this screen have a frozen `report_key`, and therefore saved views? */
export function savedViewsSupported(screen) {
  return !!(screen && screen.key);
}

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
 * The FilterSet dimensions each LEDGER route can actually honour — PER ROUTE,
 * because they do not agree and a single list made the screens claim a scope
 * three of them do not apply.
 *
 * READ `app/backend/main.py`, NOT THIS COMMENT, IF YOU CHANGE THIS. The entries
 * below are the query parameters each route DECLARES, transcribed:
 *
 *   main.py:406  dashboard(entity_id, plant_id)        — no project_id
 *   main.py:444  wbs(project_id in the PATH)           — project only
 *   main.py:601  pos()                                 — none
 *   main.py:684  grns()                                — none
 *   main.py:704  bills()                               — none
 *   main.py:735  recon(project_id)                     — project only
 *   main.py:803  capitalisation()                      — none
 *
 * WHY THIS IS NOT A COSMETIC LIST. FastAPI SILENTLY DROPS an undeclared query
 * parameter: it is neither applied nor refused, and the response is byte-for-
 * byte the response to the unfiltered request. `GET /api/dashboard` and
 * `GET /api/dashboard?project_id=PRJ-01` both return all three projects. The
 * previous single list named `project_ids` as honoured everywhere, so a screen
 * on the ledger fallback with `?project=PRJ-01` showed the WHOLE PORTFOLIO'S
 * money under an active "Project: PRJ-01" chip and an EMPTY "could not apply"
 * list. A fabricated claim about scope, on a screen showing money, is worse
 * than showing no figure at all.
 *
 * Everything a route does not honour is reported through `unappliedFilters()`
 * and named on screen. This constant is the single place that knowledge lives.
 */
export const LEDGER_HONOURS = Object.freeze({
  dashboard: Object.freeze(['entity_ids', 'plant_ids']),
  wbs: Object.freeze(['project_ids']),
  reconciliation: Object.freeze(['project_ids']),
  bills: Object.freeze([]),
  purchaseOrders: Object.freeze([]),
  grns: Object.freeze([]),
  capitalisation: Object.freeze([]),
  /* `/api/bills` for the rows AND `/api/dashboard` for the total, in one
     candidate. The honoured set is the INTERSECTION, which is empty: the rows
     are unfiltered, so the payload as a whole cannot claim an entity or plant
     narrowing even though the total half of it applied one. Under-claiming a
     scope is recoverable; over-claiming one is the defect. */
  billsWithDashboardTotal: Object.freeze([]),
});

const REPORTING_NOTE = 'This deployment does not mount the reporting endpoint this screen reads '
  + '(/api/reports/metrics, served by app/backend/api/reports.py). There is therefore no source '
  + 'for this figure and none is shown — an empty table here would read as "nothing matched", '
  + 'which is a claim about your data rather than about this build.';

/* THE AGEING BUCKETS ARE MISSING A DIMENSION, NOT A ROUTE, and the difference
   is the whole reason this note is separate. /api/reports/metrics is mounted
   and answers; what it cannot do is group by an age band. `reporting.DIMENSIONS`
   defines seven axes — entity, plant, location, project, wbs, budget_head,
   category — and not one of them is a bucket of days, so there is nothing to
   ask it for. `open_commitment_ageing` and `cwip_ageing` exist in
   `reporting.REPORT_KEYS`, which makes it possible to SAVE A VIEW of these
   screens; that is a bookmark, not a computation, and it must not be mistaken
   for one.

   Saying "this endpoint is not built yet" here would send an operator looking
   for a deployment problem that does not exist. Naming the dimension sends
   them to the one change that would make the screen work. */
const AGEING_NOTE = 'This build cannot produce an ageing distribution: /api/reports/metrics is '
  + 'mounted and answering, but the reportable dimensions it publishes at /api/reports/dimensions '
  + '(entity, plant, location, project, wbs, budget_head, category) contain no age band, so there '
  + 'is no grouping to ask it for. The un-bucketed TOTAL below is real and server-computed; the '
  + 'distribution is not shown because it cannot be assembled in this browser from a page of '
  + 'documents — the buckets would be the buckets of that page while the headings claimed to be '
  + 'the buckets of the portfolio.';

const EXPORT_NOTE = 'This deployment does not mount the export-job endpoint (/api/exports, served '
  + 'by app/backend/api/exports.py), so this screen cannot queue an export. The control is '
  + 'disabled rather than offered, because a button that silently did nothing would be worse '
  + 'than saying so.';

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
 * THIS IS THE SHAPE THE REPORTING SERVICE ACTUALLY SENDS, and it is the better
 * one: 200 with zero rows and `state: "denied"`, so the client never has to
 * distinguish a refusal from a miss by status code at all. `api/reports.py`
 * chose it deliberately — "the caller is entitled to ask, and the honest
 * answer is 'nothing you can see', which is a result and not a refusal" — and
 * a 403 would additionally have made an empty estate and a denied one tellable
 * apart by status code, which is an oracle.
 *
 * `state: "empty"` is NOT this. It is a query the caller was entitled to run,
 * over a scope that reaches records, that matched nothing — a clean bill of
 * health, and the one sentence this must never be confused with.
 *
 * The older `scope_denied` flag is still read, because the ledger fallback
 * routes are free to grow it and dropping support would silently downgrade a
 * denial to an empty table on exactly the screens that can least afford it.
 */
export function payloadScopeDenied(data) {
  if (!data || typeof data !== 'object') return false;
  if (data.state === 'denied') return String(data.detail || '') || true;
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
    return {
      known: false, asOf: null, lastSyncAt: null, stale: false, staleReason: '',
      sourceLabel: '', state: 'unknown',
    };
  }

  /* THE REPORTING SERVICE'S THREE ANSWERS, AND WHY NONE COLLAPSES.
     `reporting.freshness()` returns exactly one of:

       local         every figure was computed from documents this application
                     raised. `last_sync_at` is null and that is COMPLETE, not
                     missing — nothing syncs it, so there is no sync time to
                     report and the source label is the whole provenance.
       synced        a real watermark. Render the time.
       never_synced  a connector IS configured and has never completed a poll,
                     so mirrored documents are ABSENT rather than out of date.
                     `last_sync_at` is null here too, and the naive reading —
                     "no timestamp, so freshness unknown" — renders it exactly
                     like the `local` case, which is the one thing it must not
                     look like. It is STALE, and it is marked stale here.

     Reading only `last_sync_at` would fail twice over: it would call locally
     computed data "freshness not reported", and it would let a connector that
     has never run render as calmly as one that ran a minute ago. */
  const state = typeof meta.state === 'string' ? meta.state : '';
  if (state === 'local' || state === 'synced' || state === 'never_synced') {
    return {
      known: true,
      asOf: null,
      lastSyncAt: meta.last_sync_at || null,
      highWaterMark: meta.high_water_mark || null,
      stale: state === 'never_synced',
      staleReason: state === 'never_synced' ? String(meta.detail || '') : '',
      sourceLabel: String(meta.source_label || ''),
      state,
    };
  }

  /* The ledger fallback routes, which have no freshness contract of their own
     and may carry any of these older shapes. Unchanged. */
  const asOf = meta.as_of || meta.generated_at || meta.computed_at || null;
  const lastSyncAt = meta.last_sync_at || meta.last_synced_at || meta.synced_at || null;
  return {
    known: !!(asOf || lastSyncAt),
    asOf: asOf || null,
    lastSyncAt: lastSyncAt || null,
    highWaterMark: null,
    stale: meta.stale === true,
    staleReason: String(meta.stale_reason || meta.staleness_reason || ''),
    sourceLabel: String(meta.source_label || ''),
    state: state || 'unknown',
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
/**
 * The declared reason a route could not answer, for rendering verbatim.
 *
 * Reads `missing`/`remedy` (the integration router's shape, via
 * `unavailableNoteFrom`) AND `message`/`detail` (the reporting router's). A
 * reader is better served by "no database is configured for this process" than
 * by this module's generic sentence, and the server is the only party that
 * knows which it is.
 */
export function declaredNoteFrom(err) {
  const fromIntegration = unavailableNoteFrom(err);
  if (fromIntegration) return fromIntegration;
  const body = err && err.body && typeof err.body === 'object' ? err.body : null;
  const detail = body && typeof body.detail === 'object' ? body.detail : null;
  if (!detail) return '';
  for (const key of ['message', 'detail', 'title']) {
    if (typeof detail[key] === 'string' && detail[key].trim()) return detail[key];
  }
  return '';
}

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
      /* A refused filter is NOT fallen through. The next candidate honours
         FEWER filters than the one that just refused, so falling through would
         answer a question the reader did not ask and label it as their own
         filtered total — the precise failure `UNSUPPORTED_FILTERS` exists to
         prevent, re-created on the client. */
      const refused = classifyFilterUnavailable(err);
      if (refused) throw refused;
      if (classifyDeclaredUnavailable(err)) {
        lastAbsent = candidate;
        declaredNote = declaredNoteFrom(err) || declaredNote;
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
      /* WHY THE PREFERRED SOURCE DID NOT ANSWER, in the server's own words,
         carried onto the source that did. Without this the reader is told
         they are on the ledger and never told why — and "the reporting route
         is not mounted" and "it is mounted and has no database configured"
         are different facts with different remedies. The note was previously
         kept only for the case where EVERY candidate failed, which is the one
         case where the reader can already see something is wrong. */
      declaredNote,
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

/* The FilterSet key -> the query parameter name the ledger routes declare. */
const LEDGER_PARAM_OF = Object.freeze({
  entity_ids: 'entity_id',
  plant_ids: 'plant_id',
  project_ids: 'project_id',
});

/**
 * Ledger query params for ONE route, from the canonical FilterSet.
 *
 * Only the keys THAT ROUTE declares are sent. Appending a parameter a route
 * does not declare does not narrow anything — FastAPI drops it — it only makes
 * a request log look as though a filter were applied, which is the same lie
 * this module refuses to tell on screen.
 *
 * @param {string[]} honoured - one of the LEDGER_HONOURS entries.
 * @param {Object} filters
 */
function ledgerParams(honoured, filters) {
  const f = filters || {};
  const first = (list) => (Array.isArray(list) && list.length ? list[0] : undefined);
  const out = {};
  for (const key of honoured) {
    const param = LEDGER_PARAM_OF[key];
    if (param) out[param] = first(f[key]);
  }
  return out;
}

/**
 * Serialise a FilterSet into query parameters. Arrays stay arrays.
 *
 * Kept for callers that want the plain object; the WIRE form is `toSearch()`,
 * and the difference between them is not cosmetic — see below.
 */
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

/**
 * A FilterSet as a REPEATED-KEY search string: `?entity_ids=A&entity_ids=B`.
 *
 * THIS EXISTED BECAUSE `core/api-client.js::buildQuery` COULD NOT EXPRESS A
 * LIST AND FAILED SILENTLY WHEN ASKED TO. It built parameters with
 * `URLSearchParams.set(key, value)`, and `set` stringifies an array by joining
 * it with commas: `set('entity_ids', ['E1','E2'])` emitted
 * `entity_ids=E1%2CE2` — ONE value, the eight-character string "E1,E2".
 * `api/reports.py` declares every list filter as `list[str] | None = Query()`,
 * so FastAPI parsed that as a single entity id literally named "E1,E2",
 * matched no row, and returned `state: "empty"` — a confident, wrong,
 * unfalsifiable answer to a question that was never asked.
 *
 * `buildQuery` NOW ENCODES LISTS AS REPEATED KEYS AND DROPS AN EMPTY ARRAY, so
 * the two agree and neither is a workaround for the other. This function stays
 * for the two properties `buildQuery` does not have and should not grow:
 *
 *   * it returns a SEARCH STRING to append to a path, so a drill-down URL can
 *     be built without a params object — `request()` concatenates
 *     `basePath + path + buildQuery(params)` and `buildQuery(undefined)` is the
 *     empty string, so a path carrying its own search survives intact;
 *   * its order is DETERMINISTIC (FilterSet-key order, then list order), which
 *     is what makes a drill-down link comparable by eye with the card link it
 *     came from.
 *
 * Order is FilterSet-key order and then list order, so the same FilterSet
 * always produces the same URL — which is what makes a drill-down link
 * comparable by eye with the card link it came from.
 */
export function toSearch(filters, extra = {}) {
  const params = new URLSearchParams();
  const append = (key, value) => {
    if (value === null || value === undefined || value === '') return;
    if (Array.isArray(value)) {
      // An EMPTY array is dropped, not sent. `FilterSet._tuple` keeps `None`
      // and `()` distinct — "do not filter on this" versus "restrict to
      // nothing" — and an omitted parameter is the `None` this means. Sending
      // an empty repeated key is not possible over a query string anyway, so
      // the honest encoding of "no value chosen" is no parameter.
      for (const v of value) if (v !== null && v !== undefined && v !== '') params.append(key, String(v));
      return;
    }
    params.append(key, String(value));
  };
  for (const [key, value] of Object.entries(filters || {})) append(key, value);
  for (const [key, value] of Object.entries(extra || {})) append(key, value);
  const s = params.toString();
  return s ? `?${s}` : '';
}

/**
 * The FilterSet a reporting call actually sends: the screen's own filters,
 * with the screen's default grouping applied ONLY when the reader chose none.
 */
export function reportFilters(filters, screen) {
  const f = { ...(filters || {}) };
  const chosen = Array.isArray(f.group_by) ? f.group_by.filter(Boolean) : [];
  if (!chosen.length && screen && screen.groupBy) f.group_by = [...screen.groupBy];
  return f;
}

/**
 * A reporting-surface candidate over `/api/reports/metrics`.
 *
 * The whole FilterSet is sent as the query. The reporting service owns the
 * parse; this module owns only that the SAME object goes to cards, charts,
 * tables and exports — which is the requirement a second filter shape would
 * break.
 */
function metricsCandidate(filters, screen, extra = {}) {
  const search = toSearch(reportFilters(filters, screen), extra);
  return {
    source: 'reports',
    template: REPORT_ROUTES.metrics,
    label: REPORT_ROUTES.metrics,
    call: async () => adaptReportPayload(await reports.get(`/metrics${search}`)),
    unapplied: [],
  };
}

/* ------------------------------------------------------------------ *
 * The calls, one per screen
 * ------------------------------------------------------------------ */

/** SCR-01 / SCR-02 / SCR-04: the portfolio, as cards plus one row per project. */
export function getPortfolio(filters, screen = SCREENS.executive) {
  return firstAvailable([
    metricsCandidate(filters, screen),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.dashboard,
      label: '/api/dashboard',
      call: () => ledger.get('/dashboard', ledgerParams(LEDGER_HONOURS.dashboard, filters)),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS.dashboard),
    },
  ], REPORTING_NOTE);
}

/**
 * SCR-05 / SCR-06 / SCR-07 / SCR-08: one project's WBS, with its ledger.
 *
 * THE LEDGER ROUTE IS THE PREFERRED SOURCE HERE, AND IT IS THE ONLY PLACE IN
 * THIS FILE WHERE THAT IS TRUE. These four screens render a TREE — nested
 * children, a level per node, `carries_budget` on each — and
 * `/api/reports/metrics` cannot produce one. It answers grouped rows: group by
 * `wbs` and you get one flat row per element, with its metrics but with no
 * parent, no depth and no `carries_budget`. A tree table fed that would draw
 * every node at the root, and on a hierarchy whose whole purpose is to show a
 * rollup against its parts, a flattened tree is not a cosmetic loss — a reader
 * cannot see which figures are rollups, which is how an ancestor's value comes
 * to be read as an own-value and counted twice.
 *
 * So the hierarchy comes from `/api/projects/{project_id}/wbs`, which really
 * does serve one, and the screen states — through `unapplied` — that the route
 * narrows by project and by nothing else. That is a smaller lie than a
 * complete-looking flat table, and it is not a lie at all, because it is
 * printed on the screen.
 */
export function getWbs(projectId, filters, screen = SCREENS.wbsHierarchy) {
  if (!projectId) {
    return Promise.reject(new AnalyticsApiError(
      'Choose a CAPEX project to load its WBS hierarchy.',
      { status: 0, kind: 'validation' },
    ));
  }
  return firstAvailable([
    {
      source: 'ledger',
      template: LEDGER_ROUTES.wbs,
      label: `/api/projects/${projectId}/wbs`,
      call: () => ledger.get(`/projects/${encodeURIComponent(projectId)}/wbs`),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS.wbs),
    },
    /* Second, not first, and it will rarely be reached: if the hierarchy route
       is ever unmounted, flat grouped rows honouring the WHOLE FilterSet are
       better than no figures at all — and the source line says which one
       answered, so the reader is never left guessing which they are looking
       at. */
    metricsCandidate({ ...filters, project_ids: [projectId] }, screen),
  ], REPORTING_NOTE);
}

/**
 * SCR-19: the CWIP ledger — posted vendor bills against WBS.
 *
 * THE LEDGER PATH MAKES TWO CALLS, AND THE SECOND ONE IS THE POINT.
 * `/api/bills` returns the bill ROWS and no total. Summing those rows in the
 * browser to produce a "Total actual CWIP" card would be exactly the
 * fabrication this feature refuses — and worse than usual here, because
 * `/api/bills` includes bills in accounting states that are NOT effective
 * (voided, and anything outside `domain.ACCOUNTING_EFFECTIVE_BILL_STATES`),
 * so a naive sum of the rows would OVERSTATE actual CWIP by every voided bill
 * on the screen.
 *
 * `/api/dashboard`'s `totals.actual` is the server's own figure and already
 * excludes them. So the card comes from there, the rows come from `/api/bills`,
 * and the screen states which rows the card excludes rather than quietly
 * dropping them from the list. A reader who needs to see a voided bill can;
 * they simply cannot mistake it for CWIP.
 */
export function getCwipLedger(filters) {
  return firstAvailable([
    metricsCandidate(filters, SCREENS.cwipLedger),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.bills,
      label: '/api/bills with the total from /api/dashboard',
      call: async () => {
        const [bills, dashboard] = await Promise.all([
          // `/api/bills` declares NO query parameter (main.py:704), so none is
          // sent: the rows are the whole estate's, and `unapplied` says so.
          ledger.get('/bills', ledgerParams(LEDGER_HONOURS.bills, filters)),
          // The total is a SEPARATE, server-computed figure. If the dashboard
          // refuses or fails, the rows still render and the card renders "not
          // reported" — which is true — rather than a sum of the rows.
          ledger.get('/dashboard', ledgerParams(LEDGER_HONOURS.dashboard, filters))
            .catch(() => null),
        ]);
        return {
          rows: Array.isArray(bills) ? bills : [],
          totals: dashboard && dashboard.totals ? dashboard.totals : null,
          total_excludes_ineffective: true,
        };
      },
      unapplied: unappliedFilters(filters, LEDGER_HONOURS.billsWithDashboardTotal),
    },
  ], REPORTING_NOTE);
}

/**
 * SCR-24: the measured, un-bucketed actual-CWIP total — server-summed.
 *
 * Same shape as `getCommitmentTotal()`: the TOTAL is available today and the
 * BUCKETS are not, and the two are fetched separately so a screen can show
 * the one it has and be explicit about the one it does not.
 */
export function getCwipTotal(filters) {
  return firstAvailable([
    metricsCandidate(filters, SCREENS.cwipAgeing),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.dashboard,
      label: '/api/dashboard',
      call: () => ledger.get('/dashboard', ledgerParams(LEDGER_HONOURS.dashboard, filters)),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS.dashboard),
    },
  ], REPORTING_NOTE);
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
export async function getAgeing(kind, filters) {
  const screen = kind === 'cwip' ? SCREENS.cwipAgeing : SCREENS.commitmentAgeing;
  /* THE REFUSAL IS READ FROM THE BUILD, NOT ASSUMED.
     `/api/reports/dimensions` publishes what this build can group by, exactly
     so a screen can grey a control out with the reason attached instead of
     discovering the refusal by sending a request and getting a 422. If an
     age-band dimension is ever added, this screen starts working without a
     line changing here — and until then it says which dimension is missing
     rather than "not built yet", which was never true of this route. */
  const dimensions = await getDimensions();
  const band = (dimensions.dimensions || []).find(
    (d) => /age|ageing|aging|bucket/i.test(String(d && d.name || '')),
  );
  if (!band) {
    const err = new EndpointUnavailableError(REPORT_ROUTES.dimensions, AGEING_NOTE);
    /* The route is MOUNTED and answered. What is missing is a dimension, and
       `reason` is what stops the block saying "this deployment does not mount
       /api/reports/dimensions" about a route that just replied. */
    err.reason = 'the reportable dimensions it publishes contain no age band, so there is no '
      + 'grouping to ask it for.';
    throw err;
  }
  /* The band goes in as the FilterSet's own `group_by`, not as an extra query
     parameter. `toSearch` appends rather than replaces — it has to, because
     repeated keys are how a list travels — so passing it as an extra would
     emit the screen's grouping AND the band, and the report would be broken
     down twice. */
  return firstAvailable(
    [metricsCandidate({ ...filters, group_by: [band.name] }, screen)], AGEING_NOTE,
  );
}

/**
 * `/api/reconciliation`'s summary, in the CANONICAL METRIC VOCABULARY.
 *
 * THE TWO SOURCES NAME THE SAME QUANTITIES DIFFERENTLY, AND THE SCREEN MUST
 * ASK FOR ONE SET OF NAMES.
 *
 *   canonical (`domain._COMPONENTS`, and so `/api/dashboard`'s and
 *   `/api/reports/metrics`' totals)   `commitment`  `actual`  `received_not_billed`
 *   `/api/reconciliation`'s summary   `open_commitment_paise` `billed_paise`
 *                                     `received_not_billed_paise`
 *
 * SCR-23's tiles were keyed on the SECOND set, which is the FALLBACK source's.
 * The screen therefore worked on the degraded path and rendered "not reported"
 * on the preferred one — the failure mode that hides itself, because the
 * fallback is what a developer sees on a SQLite build. SCR-24 was keyed on the
 * canonical names and was correct on both, which is what makes this a mismatch
 * rather than a convention.
 *
 * The renaming happens HERE rather than in the screen so that exactly one
 * vocabulary reaches `renderCards`, and so that a screen never has to know
 * which source answered in order to read a figure.
 *
 * WHAT IS NOT IDENTICAL, SAID OUT LOUD. `actual` from the canonical sources is
 * the value of every accounting-effective vendor bill in scope.
 * `billed_paise` here is the subset of that value attributable to a PO LINE
 * (`domain.reconciliation` joins `bill_line.po_line_id IS NOT NULL`). A bill
 * booked with no purchase-order line behind it is in one and not the other.
 * They are the same measurement of "what has been invoiced" over two different
 * populations, and the screen states which source answered on every load, so a
 * reader can tell which of the two they are looking at. Renaming does not make
 * them equal and this comment exists so nobody later assumes it did.
 */
function canonicaliseReconciliation(raw) {
  if (!raw || typeof raw !== 'object') return raw;
  const summary = raw.summary && typeof raw.summary === 'object' ? raw.summary : null;
  const canonicalSummary = summary ? (() => {
    const { open_commitment_paise: com, billed_paise: act, ...rest } = summary;
    return {
      ...rest,
      commitment: com === undefined ? null : com,
      actual: act === undefined ? null : act,
    };
  })() : null;
  /* The ROWS keep every key they arrived with and GAIN the canonical one, so
     `assertSumsBack(total, rows, 'commitment')` can check the server's own
     arithmetic on this path too. `sumColumn` reads `row[key]` EXACTLY — it does
     no `_paise` aliasing — so the alias has to be the bare canonical name, the
     same name `/api/reports/metrics` puts on its rows. Nothing is dropped: a
     row's `open_commitment_paise` is still there for anything reading it by the
     reconciliation's own name. */
  const rows = Array.isArray(raw.rows) ? raw.rows.map((row) => (
    row && typeof row === 'object' && row.open_commitment_paise !== undefined
      ? { ...row, commitment: row.open_commitment_paise }
      : row)) : raw.rows;
  return { ...raw, summary: canonicalSummary, rows };
}

/** The measured, un-bucketed open-commitment total — server-summed. */
export function getCommitmentTotal(filters) {
  return firstAvailable([
    metricsCandidate(filters, SCREENS.commitmentAgeing),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.reconciliation,
      label: '/api/reconciliation',
      call: async () => canonicaliseReconciliation(await ledger.get(
        '/reconciliation', ledgerParams(LEDGER_HONOURS.reconciliation, filters),
      )),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS.reconciliation),
    },
  ], REPORTING_NOTE);
}

/** SCR-25: exceptions and overruns. */
export function getExceptions(filters) {
  return firstAvailable([
    metricsCandidate(filters, SCREENS.exceptions),
    {
      source: 'ledger',
      template: LEDGER_ROUTES.dashboard,
      label: '/api/dashboard',
      call: () => ledger.get('/dashboard', ledgerParams(LEDGER_HONOURS.dashboard, filters)),
      unapplied: unappliedFilters(filters, LEDGER_HONOURS.dashboard),
    },
  ], REPORTING_NOTE);
}

/**
 * The rows behind one grouped figure: the SAME FilterSet plus the clicked
 * dimension and its key.
 *
 * THE ROUND TRIP IS THE CONTRACT — "a drill-down that does not sum back to the
 * figure clicked is a defect" — and it holds here by CONSTRUCTION rather than
 * by agreement, because both sides of the comparison come from one place.
 * `reporting.drill_down` narrows through `FilterSet.narrowed_to`, which
 * INTERSECTS rather than replaces, then re-runs the SAME aggregate over the
 * SAME fact with only `group_by` changed. There is no second query and no
 * second set of formulas to drift.
 *
 * This function's whole job is to not break that: it sends the FilterSet the
 * card was built from, UNCHANGED, and adds `dimension` and `key`. It does not
 * re-read the filter bar, and it strips `cursor` — a drill-down starts at the
 * first page of its own result, and inheriting the card screen's cursor would
 * page into the middle of a different result set and return rows that cannot
 * sum back to anything.
 *
 * There is NO LEDGER FALLBACK. `/api/dashboard` applies three of the eighteen
 * filters, so its rows could not sum back to a card the reporting service
 * computed — and a drill-down that silently answers from a different
 * population is worse than one that says it is unavailable.
 *
 * @param {Object} filters - the card's FilterSet, verbatim.
 * @param {string} dimension - the clicked dimension, e.g. 'project'.
 * @param {string|null} key - the clicked key. `null` is a real value: a group
 *   whose key is NULL (a project with no plant) drills to the rows with no
 *   key, and inventing a sentinel id would fabricate a filter.
 * @param {string[]} [grain] - the finer grouping to land on.
 */
export function getDrilldown(filters, dimension, key = null, grain = null) {
  if (!dimension) {
    return Promise.reject(new AnalyticsApiError(
      'A drill-down needs the dimension that was clicked.',
      { status: 0, kind: 'validation' },
    ));
  }
  const sent = { ...(filters || {}), cursor: null };
  const extra = { dimension };
  if (key !== null && key !== undefined) extra.key = key;
  if (grain && grain.length) extra.grain = grain;
  const search = toSearch(sent, extra);
  return firstAvailable([{
    source: 'reports',
    template: REPORT_ROUTES.drillDown,
    label: REPORT_ROUTES.drillDown,
    call: async () => adaptReportPayload(await reports.get(`/drill-down${search}`)),
    unapplied: [],
  }], REPORTING_NOTE);
}

/**
 * What this build can group by, sort by, and what it cannot filter on.
 *
 * Cached for the life of the page: it describes the BUILD, not the data, so it
 * cannot change under a reader. A failure to read it degrades to the frozen
 * defaults rather than blocking a screen — but the failure is reported in
 * `known`, so a caller can tell "this build has no age-band dimension" from
 * "the catalogue could not be read".
 */
let dimensionsPromise = null;

export function getDimensions() {
  if (dimensionsPromise) return dimensionsPromise;
  dimensionsPromise = (async () => {
    try {
      const present = await available(REPORT_ROUTES.dimensions);
      if (present === false) throw new Error('not mounted');
      const data = await reports.get('/dimensions');
      return {
        known: true,
        dimensions: Array.isArray(data.dimensions) ? data.dimensions : [],
        metrics: Array.isArray(data.metrics) ? data.metrics : [],
        sortable: Array.isArray(data.sortable) ? data.sortable : [],
        reportKeys: Array.isArray(data.report_keys) ? data.report_keys : [],
        unavailableFilters: Array.isArray(data.unavailable_filters)
          ? data.unavailable_filters : [],
      };
    } catch {
      return {
        known: false,
        dimensions: GROUPABLE.map((name) => ({ name, description: '' })),
        metrics: [], sortable: [], reportKeys: Object.values(REPORT_KEYS),
        unavailableFilters: [],
      };
    }
  })();
  return dimensionsPromise;
}

/** Test seam: forget the cached dimension catalogue. */
export function resetDimensions() { dimensionsPromise = null; }

/**
 * Queue an export of exactly what is on screen.
 *
 * Returns `{ queued: false, reason }` rather than throwing when the export
 * route is not mounted, so a screen can DISABLE the control and say why
 * instead of offering a button that fails when pressed.
 *
 * THE BODY IS `{dataset, filters}` AND THERE IS NO `report_id` AND NO
 * `format`. `api/exports.py` declares `CreateExportRequest` with
 * `extra="forbid"`, so a stray field is a 422 rather than a silently ignored
 * one; and the output format is the dataset's, not the caller's — every
 * dataset in this build is CSV and offering a choice would imply one exists.
 *
 * `cursor`, `limit`, `sort` and `group_by` are STRIPPED, and this is the one
 * place in this feature where a filter is removed rather than sent. They are
 * not narrowing filters: they are this screen's paging and shape, and
 * `exports.FILTER_FIELDS_NOT_APPLICABLE_TO_AN_EXPORT` refuses all four
 * outright, because an export delivers the whole result set in a captured
 * column and row order. Sending the screen's `limit` would either be refused
 * or — worse, had the backend been laxer — truncate the file to one page while
 * the reader believed they had the portfolio.
 */
export async function queueExport(screen, filters) {
  const dataset = (screen && screen.dataset) || SCREENS.executive.dataset;
  const present = await available(EXPORT_ROUTES.create);
  if (present === false) {
    return { queued: false, reason: EXPORT_NOTE, template: EXPORT_ROUTES.create };
  }
  const sent = { ...(filters || {}) };
  for (const shapeKey of ['cursor', 'limit', 'sort', 'group_by']) delete sent[shapeKey];
  const body = { dataset, filters: toQuery(sent) };
  const result = await firstAvailable([{
    source: 'exports',
    template: EXPORT_ROUTES.create,
    label: EXPORT_ROUTES.create,
    call: () => ledger.post('/exports', body),
  }], EXPORT_NOTE);
  return { queued: true, ...result };
}

/** Poll one export job. 202 at creation means the job id is the only answer. */
export function getExportJob(exportJobId) {
  return ledger.get(`/exports/${encodeURIComponent(exportJobId)}`);
}

/** Is an export possible in this build at all? Used to gate the control. */
export async function exportAvailable() {
  return available(EXPORT_ROUTES.create);   // unknown stays unknown
}

/* ------------------------------------------------------------------ *
 * Saved views
 * ------------------------------------------------------------------ */

/** Every saved view this caller may open for one screen. */
export async function listViews(screen) {
  if (!savedViewsSupported(screen)) {
    return { state: 'unsupported', views: [], default_view_id: null };
  }
  return reports.get(`/views${toSearch({ report_key: screen.key })}`);
}

/**
 * Save the CURRENT FilterSet as a named view.
 *
 * The definition is the FilterSet in `FilterSet.to_json`'s shape, and `cursor`
 * is excluded — a saved view that stored one would open on page four of a
 * result set that no longer exists. `FilterSet.from_json` REFUSES an unknown
 * key rather than dropping it, so a definition is either applied whole or not
 * at all.
 */
export function saveView(screen, { entityId, name, description, visibility, filters }) {
  if (!savedViewsSupported(screen)) {
    return Promise.reject(new AnalyticsApiError(
      'This screen has no saved-view key, so a view cannot be saved against it.',
      { status: 0, kind: 'validation' },
    ));
  }
  const definition = { ...toQuery(filters) };
  delete definition.cursor;
  return reports.post('/views', {
    entity_id: entityId,
    report_key: screen.key,
    name,
    description: description || null,
    visibility: visibility || 'PRIVATE',
    definition,
  });
}

export const NOTES = Object.freeze({
  reporting: REPORTING_NOTE, exporting: EXPORT_NOTE, ageing: AGEING_NOTE,
});
