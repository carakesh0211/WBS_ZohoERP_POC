/* app/frontend/src/features/settings/fx-api.js
   Exchange-rate administration API surface (Fable 5.1, migration 028),
   the client for app/backend/api/fx_admin.py. Same shape as
   features/budget/budget-api.js: a base path, an error class, this
   family's wording, and the endpoint signatures -- the session header,
   correlation id, RFC-7807 parsing and not-found-over-forbidden rule all
   live once in core/api-client.js.

     GET  /api/fx/rates?source=&target=&active=&status=&rate_source=
                       &effective_from=&effective_to=&cursor=&limit=
     POST /api/fx/rates                         (fx.manage, Idempotency-Key)
     GET  /api/fx/rates/lookup?source=&target=&date=&rate_source=&amount_minor=
     GET  /api/fx/rates/history?source=&target=
     POST /api/fx/rates/import/preview          (fx.manage)
     POST /api/fx/rates/import                  (fx.manage, Idempotency-Key)
     GET  /api/fx/rates/{id}
     POST /api/fx/rates/{id}/activate           (fx.manage)
     POST /api/fx/rates/{id}/deactivate         (fx.manage)

   A rate is an EXACT DECIMAL STRING on the wire in both directions. Nothing
   in this module or its screen parses one into a Number: the server
   validates and quantises (fx.parse_rate), and the screen shows the string
   it was given. The one integer that crosses is `amount_minor`, in the
   source currency's own minor units, for the translation preview.
*/

import { ApiClientError, createApiClient } from '../../core/api-client.js';

export class FxApiError extends ApiClientError {
  constructor(message, opts) {
    super(message, opts);
    this.name = 'FxApiError';
  }
}

const client = createApiClient({
  basePath: '/api/fx',
  ErrorClass: FxApiError,
  messages: {
    network: 'The exchange-rate service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to view exchange rates.',
    notfound: 'No exchange rate was found for this request.',
    error: (status) => `The exchange-rate service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The exchange-rate service returned a response that could not be understood.',
  },
});

/** GET /api/fx/rates */
export function listRates({ source, target, active, status, rateSource, effectiveFrom, effectiveTo, cursor, limit } = {}) {
  return client.get('/rates', {
    source: source || undefined,
    target: target || undefined,
    active: active === undefined || active === null ? undefined : String(active),
    status: status || undefined,
    rate_source: rateSource || undefined,
    effective_from: effectiveFrom || undefined,
    effective_to: effectiveTo || undefined,
    cursor: cursor || undefined,
    limit: limit || undefined,
  });
}

/** GET /api/fx/rates/{id} */
export function getRate(fxRateId) {
  return client.get(`/rates/${encodeURIComponent(fxRateId)}`);
}

/** POST /api/fx/rates (fx.manage). `values.rate` is a decimal STRING. */
export function createRate(values, idempotencyKey) {
  return client.request('POST', '/rates', { body: values, headers: { 'Idempotency-Key': idempotencyKey || '' } });
}

/** POST /api/fx/rates/{id}/activate (fx.manage) */
export function activateRate(fxRateId) {
  return client.post(`/rates/${encodeURIComponent(fxRateId)}/activate`, {});
}

/** POST /api/fx/rates/{id}/deactivate (fx.manage) */
export function deactivateRate(fxRateId, reason) {
  return client.post(`/rates/${encodeURIComponent(fxRateId)}/deactivate`, { reason });
}

/** GET /api/fx/rates/lookup -- the applicable ACTIVE rate, or the coded refusal
 * (FX_RATE_UNAVAILABLE as a 404, FX_RATE_AMBIGUOUS as a 409). */
export function lookupRate({ source, target, date, rateSource, amountMinor } = {}) {
  return client.get('/rates/lookup', {
    source,
    target: target || undefined,
    date,
    rate_source: rateSource || undefined,
    amount_minor: amountMinor === undefined || amountMinor === null || amountMinor === '' ? undefined : amountMinor,
  });
}

/** GET /api/fx/rates/history -- every row ever recorded for a pair, in every state. */
export function getHistory({ source, target, limit } = {}) {
  return client.get('/rates/history', { source, target: target || undefined, limit: limit || undefined });
}

/** POST /api/fx/rates/import/preview (fx.manage) */
export function previewImport(rows) {
  return client.post('/rates/import/preview', { rows });
}

/** POST /api/fx/rates/import (fx.manage, all-or-nothing, Idempotency-Key) */
export function commitImport(rows, idempotencyKey) {
  return client.request('POST', '/rates/import', { body: { rows }, headers: { 'Idempotency-Key': idempotencyKey || '' } });
}
