/* app/frontend/src/features/budget/budget-api.js
   Budget API surface (SCR-09 Budget Planning Grid, SCR-10 Budget Version
   Comparison, SCR-13 Budget Availability Check).

   This module used to carry its own copy of the session header, correlation
   id, RFC-7807 parsing and not-found-over-forbidden rule, because core/api.js
   was hard-wired to the audit base path and could not be reused. Wave 3
   parameterised that client — core/api-client.js — so all of it lives once.
   What remains here is only the contract: a base path, an error class, this
   family's wording, and the endpoint signatures.

   The frozen contract is docs/WAVE2_CONTRACTS.md ("API contract — Budget").
   No field is invented here that is not in that contract.

     GET /api/budget/cells?project_id=&wbs_id=&budget_head_id=&cursor=&limit=
       -> {"items":[{"wbs_id","wbs_path","budget_head_id",
                     "budget_paise","original_paise","revisions_paise","future_budget_paise",
                     "ordered_paise","commitment_paise","actual_paise","received_paise",
                     "received_not_billed_paise","pr_reserved_paise",
                     "exposure_paise","available_paise","recomputed_at"}],
           "next_cursor","has_more"}

     GET /api/budget/availability?wbs_id=&budget_head_id=&amount_paise=
       -> {"wbs_id","budget_head_id","owning_wbs_id",
           "budget_paise","exposure_paise","available_paise","requested_paise",
           "verdict":"OK|WATCH|CRITICAL|EXCEEDS_BUDGET",
           "shortfall_paise","checked_at"}

     GET /api/budget/lines?project_id=&wbs_id=&version=
       -> {"items":[{"budget_line_id","wbs_id","budget_head_id",
                     "kind":"ORIGINAL|REVISION|TRANSFER","amount_paise",
                     "effective_from","effective_to","status","justification",
                     "created_at","created_by","version_no"}]}

     GET /api/budget/versions?project_id=
       -> {"items":[{"version","label","created_at","total_paise"}]}
     GET /api/budget/compare?project_id=&left=&right=
       -> {"rows":[{"wbs_id","budget_head_id","left_paise","right_paise","delta_paise"}]}

   Original-budget creation (SCR "Budget Setup") and the budget-category
   master, per app/backend/api/budgets_original.py's own docstring:

     GET  /api/budget/categories?include_inactive=&entity_id=
       -> {"items":[{"category_id","code","name","description",
                     "parent_category_id","display_order","active",
                     "entity_id","effective_from","effective_to","version_no"}]}
     POST /api/budget/categories                                budget.category.manage
     PUT  /api/budget/categories/{id}                            budget.category.manage
       body carries expected_version; a mismatch answers 409 VERSION_CONFLICT.
     POST /api/budget/categories/{id}/deactivate                 budget.category.manage

     GET /api/budget/selectors?kind=&q=&project_id=&entity_id=&limit=
       -> {"kind","items":[{"id","label",...}]}
       kind is one of entity|project|wbs|budget_head|budget_category|division|
       branch|zone|plant|location|period|fiscal_year. `wbs` REQUIRES project_id.

     GET /api/budget/custom-fields
       -> {"applies_to","items":[{"code","label",
                                  "data_type":"TEXT|NUMBER|DATE|BOOLEAN|SELECT",
                                  "select_options","is_required"}]}

     GET  /api/budget/originals?project_id=&status=&cursor=&limit=
       -> {"items":[{"budget_id","budget_number","capex_code","project_name",
                     "fiscal_year","title","status","total_paise","line_count",
                     "created_by","version_no"}],"next_cursor"}
     POST /api/budget/originals                                  budget.create
       header Idempotency-Key; body {project_id,fiscal_year,title,period_id?,
       justification?,custom_fields{},lines:[{wbs_id,budget_head_id,
       budget_category_id,amount_paise,justification?,division_id?,branch_id?,
       zone_id?,plant_id?,location_id?,custom_fields{}}]}
       -> 201 {budget_id,budget_number,status,version_no,lines:[...],total_paise}
     GET  /api/budget/originals/{id}
     PUT  /api/budget/originals/{id}                              budget.create
       body carries expected_version.
     POST /api/budget/originals/{id}/submit                       budget.create
       body {expected_version?} -> {status:"SUBMITTED",approval_instance_id}
       or a 409 problem.
     POST /api/budget/originals/{id}/cancel                       budget.create
       body {reason}.
     GET  /api/budget/originals/{id}/audit -> {entries:[{seq,at,actor,action,detail}]}

     GET  /api/budget/originals/import/template  -> text/csv
     POST /api/budget/originals/import/preview
       body {project_id,csv_text} -> {rows,problems:[{row,column,code,message}],
       valid,total_paise,lines}
     POST /api/budget/originals/import                            budget.create
       header Idempotency-Key; body {project_id,fiscal_year,title,csv_text,
       period_id?,justification?,custom_fields{}} -> 201 document

   Errors: RFC-7807 {detail:{code,title,status,detail,problems?}}. A 422
   BUDGET_LINES_INVALID carries `problems:[{line,field,code,message}]`, read
   by budget-setup.js to map each problem back onto the offending line editor.
   A 503 DATABASE_NOT_CONFIGURED means no PostgreSQL is wired up; the caller
   renders the same "unavailable" state every other screen here uses for it.

   Session: see core/api-client.js. sessionStorage only, never localStorage.
*/

import { ApiClientError, createApiClient } from '../../core/api-client.js';

export class BudgetApiError extends ApiClientError {
  /**
   * @param {string} message - safe to show a user directly.
   * @param {Object} [opts] - see ApiClientError.
   *
   * 'notfound' covers both a genuinely missing object AND an out-of-scope
   * read the server intentionally answers as 404 rather than 403, per the UI
   * contract's anti-oracle rule — a 403 on an id is an existence oracle. Every
   * endpoint here is a GET, so the shared client maps a 403 to 'notfound' too.
   */
  constructor(message, opts) {
    super(message, opts);
    this.name = 'BudgetApiError';
  }
}

const client = createApiClient({
  basePath: '/api/budget',
  ErrorClass: BudgetApiError,
  messages: {
    network: 'The budget service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to view budget data.',
    // Deliberately generic and identical whether the object never existed or
    // is simply out of this user's scope.
    notfound: 'No budget data was found for these filters.',
    error: (status) => `The budget service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The budget service returned a response that could not be understood.',
  },
});

/**
 * GET /api/budget/cells
 * @param {Object} [filters]
 * @param {string} [filters.projectId]
 * @param {string} [filters.wbsId]
 * @param {string} [filters.budgetHeadId]
 * @param {string} [filters.cursor]
 * @param {number} [filters.limit]
 */
export function listCells({ projectId, wbsId, budgetHeadId, cursor, limit } = {}) {
  return client.get('/cells', {
    project_id: projectId,
    wbs_id: wbsId,
    budget_head_id: budgetHeadId,
    cursor,
    limit,
  });
}

/**
 * GET /api/budget/availability
 * @param {Object} args
 * @param {string} args.wbsId
 * @param {string} args.budgetHeadId
 * @param {number} args.amountPaise
 */
export function checkAvailability({ wbsId, budgetHeadId, amountPaise }) {
  return client.get('/availability', {
    wbs_id: wbsId,
    budget_head_id: budgetHeadId,
    amount_paise: amountPaise,
  });
}

/**
 * GET /api/budget/lines
 * @param {Object} [filters]
 * @param {string} [filters.projectId]
 * @param {string} [filters.wbsId]
 * @param {string} [filters.version]
 */
export function listLines({ projectId, wbsId, version } = {}) {
  return client.get('/lines', { project_id: projectId, wbs_id: wbsId, version });
}

/** GET /api/budget/versions?project_id= (SCR-10 comparison) */
export function listVersions(projectId) {
  return client.get('/versions', { project_id: projectId });
}

/**
 * GET /api/budget/compare
 * @param {Object} args
 * @param {string} args.projectId
 * @param {string} args.left
 * @param {string} args.right
 */
export function compareVersions({ projectId, left, right }) {
  return client.get('/compare', { project_id: projectId, left, right });
}

/* ==================================================================
   Budget Setup: budget categories, governed selectors, custom fields
   and original-budget documents. Same client, same error class -- these
   are still /api/budget/*.
   ================================================================== */

/** GET /api/budget/categories?include_inactive=&entity_id= */
export function listCategories({ includeInactive, entityId } = {}) {
  return client.get('/categories', { include_inactive: includeInactive || undefined, entity_id: entityId });
}

/** POST /api/budget/categories (budget.category.manage) */
export function createCategory(values) {
  return client.post('/categories', values);
}

/** PUT /api/budget/categories/{id} (budget.category.manage) — values must carry expected_version. */
export function updateCategory(categoryId, values) {
  return client.put(`/categories/${encodeURIComponent(categoryId)}`, values);
}

/** POST /api/budget/categories/{id}/deactivate (budget.category.manage) */
export function deactivateCategory(categoryId) {
  return client.post(`/categories/${encodeURIComponent(categoryId)}/deactivate`, {});
}

/**
 * GET /api/budget/selectors — the governed pickers behind every
 * components/budget/governed-select.js instance.
 * @param {Object} args
 * @param {'entity'|'project'|'wbs'|'budget_head'|'budget_category'|'division'|
 *   'branch'|'zone'|'plant'|'location'|'period'|'fiscal_year'} args.kind
 * @param {string} [args.q]
 * @param {string} [args.projectId] - REQUIRED when kind === 'wbs'.
 * @param {string} [args.entityId]
 * @param {number} [args.limit]
 */
export function listSelectors({
  kind, q, projectId, entityId, limit,
}) {
  return client.get('/selectors', {
    kind, q, project_id: projectId, entity_id: entityId, limit,
  });
}

/** GET /api/budget/custom-fields */
export function listCustomFields() {
  return client.get('/custom-fields');
}

/** GET /api/budget/originals?project_id=&status=&cursor=&limit= */
export function listOriginals({
  projectId, status, cursor, limit,
} = {}) {
  return client.get('/originals', {
    project_id: projectId, status, cursor, limit,
  });
}

/** GET /api/budget/originals/{id} */
export function getOriginal(budgetId) {
  return client.get(`/originals/${encodeURIComponent(budgetId)}`);
}

/**
 * POST /api/budget/originals (budget.create).
 * @param {Object} payload - {project_id, fiscal_year, title, period_id?,
 *   justification?, custom_fields, lines}
 * @param {string} idempotencyKey - regenerated by the caller after success.
 */
export function createOriginal(payload, idempotencyKey) {
  return client.request('POST', '/originals', { body: payload, headers: { 'Idempotency-Key': idempotencyKey || '' } });
}

/** PUT /api/budget/originals/{id} (budget.create) — payload must carry expected_version. */
export function updateOriginal(budgetId, payload) {
  return client.put(`/originals/${encodeURIComponent(budgetId)}`, payload);
}

/** POST /api/budget/originals/{id}/submit (budget.create) */
export function submitOriginal(budgetId, expectedVersion) {
  return client.post(`/originals/${encodeURIComponent(budgetId)}/submit`,
    expectedVersion === undefined ? {} : { expected_version: expectedVersion });
}

/** POST /api/budget/originals/{id}/cancel (budget.create) */
export function cancelOriginal(budgetId, reason) {
  return client.post(`/originals/${encodeURIComponent(budgetId)}/cancel`, { reason });
}

/** GET /api/budget/originals/{id}/audit */
export function getOriginalAudit(budgetId) {
  return client.get(`/originals/${encodeURIComponent(budgetId)}/audit`);
}

/** GET /api/budget/originals/import/template (text/csv, budget.create) */
export async function getImportTemplate() {
  const sid = (() => { try { return sessionStorage.getItem('capex.session_id') || ''; } catch { return ''; } })();
  const headers = { Accept: 'text/csv' };
  if (sid) headers['X-Session'] = sid;
  const res = await fetch('/api/budget/originals/import/template', { headers });
  if (!res.ok) {
    throw new BudgetApiError('The import template could not be downloaded.', { status: res.status, kind: 'error' });
  }
  return res.text();
}

/** POST /api/budget/originals/import/preview (budget.create) */
export function previewImport({ projectId, csvText }) {
  return client.post('/originals/import/preview', { project_id: projectId, csv_text: csvText });
}

/**
 * POST /api/budget/originals/import (budget.create, all-or-nothing draft).
 * @param {Object} args - {projectId, fiscalYear, title, csvText, periodId?, justification?, customFields?}
 * @param {string} idempotencyKey
 */
export function commitImport({
  projectId, fiscalYear, title, csvText, periodId, justification, customFields,
}, idempotencyKey) {
  return client.request('POST', '/originals/import', {
    body: {
      project_id: projectId,
      fiscal_year: fiscalYear,
      title,
      csv_text: csvText,
      period_id: periodId || undefined,
      justification: justification || undefined,
      custom_fields: customFields || {},
    },
    headers: { 'Idempotency-Key': idempotencyKey || '' },
  });
}
