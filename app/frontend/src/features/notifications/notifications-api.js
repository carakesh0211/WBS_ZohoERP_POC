/* app/frontend/src/features/notifications/notifications-api.js
   The Administrator outbox surface for app/backend/api/notifications.py
   (Stream E). Same shape as every other feature client here: a base path,
   an error class, this family's wording — the session header, correlation
   id, RFC-7807 parsing and not-found-over-forbidden rule live once in
   core/api-client.js.

     GET  /api/notifications?state=&event=&recipient_user_id=&limit=
       -> {items:[...outbox rows...], monitoring:{...}, events:[...]}
     GET  /api/notifications/{id}       -> the row + body_text + deliveries[]
     POST /api/notifications/dispatch   {limit}  -> drains what is due
     POST /api/notifications/{id}/retry -> re-queues a DEAD/FAILED row

   Every route here requires admin.reset (Administrator); the 403 a lesser
   role receives is rendered exactly as this client reports it, never
   pre-empted by hiding the screen — see the Governance nav row app.js adds,
   which is presentational only.
*/

import { ApiClientError, createApiClient } from '../../core/api-client.js';

export class NotificationsApiError extends ApiClientError {
  constructor(message, opts) {
    super(message, opts);
    this.name = 'NotificationsApiError';
  }
}

const client = createApiClient({
  basePath: '/api/notifications',
  ErrorClass: NotificationsApiError,
  messages: {
    network: 'The notification service could not be reached. Check your connection and try again.',
    auth: 'Your session has ended. Sign in again to continue.',
    notfound: 'That notification could not be found.',
    forbidden: 'Your role cannot see the notification outbox. This screen needs the Administrator role.',
    validation: 'That request could not be accepted. Correct the highlighted fields and try again.',
    error: (status) => `The notification service returned an unexpected error (HTTP ${status}).`,
    unreadable: 'The notification service returned a response that could not be understood.',
  },
});

/** GET /api/notifications — items, monitoring and the known events, together. */
export function listOutbox({ state, event, recipientUserId, limit } = {}) {
  return client.get('', {
    state: state || undefined,
    event: event || undefined,
    recipient_user_id: recipientUserId || undefined,
    limit,
  });
}

/** GET /api/notifications/{id} — the row plus its delivery attempts. */
export function getNotification(notificationId) {
  return client.get(`/${encodeURIComponent(notificationId)}`);
}

/** POST /api/notifications/dispatch {limit} — drains what is due now. */
export function dispatchDue(limit = 50) {
  return client.post('/dispatch', { limit });
}

/** POST /api/notifications/{id}/retry — DEAD or FAILED only. */
export function retryNotification(notificationId) {
  return client.post(`/${encodeURIComponent(notificationId)}/retry`, {});
}
