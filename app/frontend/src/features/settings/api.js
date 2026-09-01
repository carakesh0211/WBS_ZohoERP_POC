/* app/frontend/src/features/settings/api.js
   Settings-and-masters API surface — see docs/WAVE2_CONTRACTS.md
   ("API contract — Settings and masters").

   This module used to carry a third copy of the session header, correlation
   id, RFC-7807 parsing and status classification. Wave 3 moved all of it into
   the base-path-parameterised core/api-client.js; what remains here is the
   contract only.

   This family spans TWO base paths, /api/settings and /api/masters, so its
   client is built with an empty base path and each call site names the full
   path. That is exactly the case the old hard-wired core/api.js could not
   express.

   Contract (do not invent fields):
     {collection} in organisations | entities | divisions | branches | zones
                    | plants | locations | departments

     GET  /api/settings/{collection}?cursor=&limit=&q=&is_active=
       -> {"items":[{ "<id field>", "code","name", ...,
                       "is_active","created_at","created_by",
                       "updated_at","updated_by","version_no"}],
           "next_cursor","has_more"}
     POST /api/settings/{collection}                 -> the created row
     PUT  /api/settings/{collection}/{id}             -> the updated row
          body MUST carry version_no; mismatch -> 409 VERSION_CONFLICT
     POST /api/settings/{collection}/{id}/deactivate  -> {"id","is_active":false}

     GET  /api/masters/items | /api/masters/vendors
          ?cursor=&limit=&q=&source=&mapping_status=&is_active=
       -> {"items":[{ "item_id"|"vendor_id","code","name",
                       "source":"LOCAL|IMPORT|ZOHO",
                       "external_source","external_id","external_last_modified",
                       "source_of_truth_status","duplicate_of","mapping_status",
                       "is_active","version_no", ...}],
           "next_cursor","has_more"}
     POST /api/masters/{kind}                  source forced to LOCAL
     PUT  /api/masters/{kind}/{id}              refuses to edit a ZOHO-owned field
     POST /api/masters/{kind}/{id}/deactivate
     GET  /api/masters/{kind}/duplicates
       -> {"items":[{"id","code","name","duplicate_of","reason"}]}

   Vendor tax identity (gst_no, pan_no) arrives masked from the server by
   contract; this module never attempts to reconstruct, mask or unmask any
   value — whatever the server sends is rendered exactly as sent.

   Session: see core/api-client.js. sessionStorage only, never localStorage.
*/

import { ApiClientError, createApiClient } from '../../core/api-client.js';

export class SettingsApiError extends ApiClientError {
  /**
   * @param {string} message - safe to show a user directly.
   * @param {Object} [opts] - see ApiClientError.
   *
   * `kind` is 'network' | 'auth' | 'notfound' | 'forbidden' | 'validation' |
   * 'conflict' | 'error'. This is the one family with writes, so it is the one
   * family where 'forbidden' can appear: the shared client renders a 403 on a
   * GET as 'notfound' (an out-of-scope read must not be an existence oracle)
   * and a 403 on a write as 'forbidden' (an action the user named themselves
   * leaks no id, and "not found" would be a lie about a permission refusal).
   */
  constructor(message, opts) {
    super(message, opts);
    this.name = 'SettingsApiError';
  }
}

const client = createApiClient({
  // Empty: this family spans /api/settings and /api/masters, so each call site
  // below supplies the full path.
  basePath: '',
  ErrorClass: SettingsApiError,
  messages: {
    network: 'The settings service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to continue.',
    notfound: 'The requested record was not found.',
    forbidden: 'You do not have permission to make this change. Contact an administrator if you need access.',
    conflict: 'Someone else changed this record since it was loaded. Reload to see the latest version before trying again.',
    validation: 'The submitted values could not be accepted. Correct the highlighted fields and try again.',
    error: (status) => `The settings service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The settings service returned a response that could not be understood.',
  },
});

/* ---------------- /api/settings/{collection} ---------------- */

export function listSettings(collection, { cursor, limit, q, isActive } = {}) {
  return client.get(`/api/settings/${collection}`, {
    cursor, limit, q, is_active: isActive,
  });
}

export function createSetting(collection, body) {
  return client.post(`/api/settings/${collection}`, body);
}

export function updateSetting(collection, id, body) {
  return client.put(`/api/settings/${collection}/${encodeURIComponent(id)}`, body);
}

export function deactivateSetting(collection, id) {
  return client.post(`/api/settings/${collection}/${encodeURIComponent(id)}/deactivate`, {});
}

/* ---------------- /api/masters/{kind} ---------------- */

export function listMasters(kind, {
  cursor, limit, q, source, mappingStatus, isActive,
} = {}) {
  return client.get(`/api/masters/${kind}`, {
    cursor, limit, q, source, mapping_status: mappingStatus, is_active: isActive,
  });
}

export function createMaster(kind, body) {
  return client.post(`/api/masters/${kind}`, body);
}

export function updateMaster(kind, id, body) {
  return client.put(`/api/masters/${kind}/${encodeURIComponent(id)}`, body);
}

export function deactivateMaster(kind, id) {
  return client.post(`/api/masters/${kind}/${encodeURIComponent(id)}/deactivate`, {});
}

export function listDuplicates(kind) {
  return client.get(`/api/masters/${kind}/duplicates`);
}
