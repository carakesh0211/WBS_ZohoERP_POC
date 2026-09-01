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
