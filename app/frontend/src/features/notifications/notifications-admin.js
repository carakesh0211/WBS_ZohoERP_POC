/* app/frontend/src/features/notifications/notifications-admin.js
   The Administrator's notification outbox screen (Stream E): the monitoring
   strip, the outbox table with filters, a detail view with the delivery
   attempts, and Dispatch / Retry. Mounted as app.js's V.notifications, a
   plain NAV row under Governance (need: ['admin.reset']) — the same pattern
   the IMR register already uses for a screen this shell hosts without a
   src/core/router.js SCREENS entry.

   app.js's own `flash()` is a classic-script top-level FUNCTION declaration,
   which lands on `window` — the same `shell()` seam imr-register.js already
   uses to reach it, so a success message on Retry lands in the shell's
   ordinary flash banner rather than this module inventing a second one.
   Everything else here builds real DOM through core/dom.js's h()/text(),
   which escapes structurally, so no separate esc() is needed.
*/

import { h, text, clear } from '../../core/dom.js';
import {
  listOutbox, getNotification, dispatchDue, retryNotification, NotificationsApiError,
} from './notifications-api.js';

function shell() { return (typeof window !== 'undefined' ? window : {}); }
function announce(kind, message) { const w = shell(); if (typeof w.flash === 'function') w.flash(kind, message); }

const STATE_ROLE = {
  QUEUED: 'progress', SENDING: 'progress', SENT: 'positive',
  FAILED: 'warning', DEAD: 'negative', SUPPRESSED: 'neutral',
};
const SYM = { positive: '✔', negative: '✖', warning: '!', progress: '◐', neutral: '·' };
function stateBadge(s) {
  const role = STATE_ROLE[s] || 'neutral';
  return h('span', { class: `status st-${role}` }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, SYM[role]),
    text(s || '—'),
  ]);
}

function errorBox(err) {
  const message = err instanceof NotificationsApiError ? err.message
    : (err && err.message) || 'This request could not be completed.';
  return h('div', { class: 'msg msg-error', role: 'alert' }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
    h('div', { class: 'body' }, message),
  ]);
}

function field(labelText, id, el) {
  return h('div', { class: 'field' }, [h('label', { for: id }, labelText), el]);
}

const state = { view: 'list', selectedId: null, filters: { state: '', event: '', recipientUserId: '' } };

function monitoringStrip(monitoring) {
  if (!monitoring) return null;
  const counts = monitoring.counts || {};
  const cells = Object.keys(counts).map((k) => h('div', { class: 'notif-monitor-cell' }, [
    h('div', { class: 'k' }, k),
    h('div', { class: 'v' }, String(counts[k])),
  ]));
  cells.push(h('div', { class: 'notif-monitor-cell' }, [
    h('div', { class: 'k' }, 'Adapter'),
    h('div', { class: 'v' }, monitoring.adapter || '—'),
  ]));
  cells.push(h('div', { class: 'notif-monitor-cell' }, [
    h('div', { class: 'k' }, 'Availability'),
    h('div', { class: 'v' }, monitoring.adapter_available ? 'Available' : 'Unavailable'),
  ]));
  return h('div', { class: 'notif-monitor-strip', id: 'notifMonitorStrip' }, [
    ...cells,
    !monitoring.adapter_available && monitoring.adapter_reason
      ? h('div', { class: 'msg msg-warning', role: 'note' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
        h('div', { class: 'body' }, `Mail adapter unavailable: ${monitoring.adapter_reason}`),
      ])
      : null,
    monitoring.oldest_due
      ? h('div', { class: 'muted small' }, `Oldest item due: ${monitoring.oldest_due}`)
      : null,
  ].filter(Boolean));
}

async function renderList(root) {
  clear(root);
  const toolbar = h('div', { class: 'toolbar' });
  const monitorHost = h('div');
  const errHost = h('div');
  const tableHost = h('div', { class: 'table-wrap' });
  const dispatchResult = h('div', { id: 'notifDispatchResult' });

  const stateSelect = h('select', { id: 'notifFilterState' }, [
    h('option', { value: '' }, 'All states'),
    ...['QUEUED', 'SENDING', 'SENT', 'FAILED', 'DEAD', 'SUPPRESSED'].map((s) => h('option', {
      value: s, selected: s === state.filters.state,
    }, s)),
  ]);
  const eventInput = h('input', {
    id: 'notifFilterEvent', type: 'text', autocomplete: 'off', value: state.filters.event,
  });
  const recipientInput = h('input', {
    id: 'notifFilterRecipient', type: 'text', autocomplete: 'off', value: state.filters.recipientUserId,
  });
  const applyBtn = h('button', { type: 'button', class: 'btn-sm' }, 'Apply filters');
  const dispatchBtn = h('button', { type: 'button', class: 'btn-primary btn-sm' }, 'Dispatch due notifications');

  toolbar.appendChild(field('State', 'notifFilterState', stateSelect));
  toolbar.appendChild(field('Event', 'notifFilterEvent', eventInput));
  toolbar.appendChild(field('Recipient user id', 'notifFilterRecipient', recipientInput));
  toolbar.appendChild(h('div', { class: 'field field-action' }, applyBtn));
  toolbar.appendChild(h('div', { class: 'field field-action' }, dispatchBtn));

  root.appendChild(h('div', { class: 'imr-screen' }, [
    toolbar, errHost, monitorHost, dispatchResult, tableHost,
  ]));

  applyBtn.addEventListener('click', () => {
    state.filters = {
      state: stateSelect.value, event: eventInput.value.trim(), recipientUserId: recipientInput.value.trim(),
    };
    load();
  });

  dispatchBtn.addEventListener('click', async () => {
    dispatchBtn.disabled = true;
    clear(dispatchResult);
    try {
      const result = await dispatchDue(50);
      dispatchResult.appendChild(h('div', { class: 'msg msg-success', role: 'status' }, [
        h('span', { class: 'ico', 'aria-hidden': 'true' }, '✔'),
        h('div', { class: 'body' },
          `Dispatched: claimed ${result.claimed}, sent ${result.sent}, retry ${result.retry}, dead ${result.dead} (adapter: ${result.provider}).`),
      ]));
      load();
    } catch (err) {
      dispatchResult.appendChild(errorBox(err));
    } finally {
      dispatchBtn.disabled = false;
    }
  });

  async function load() {
    clear(errHost);
    clear(monitorHost);
    tableHost.appendChild(h('div', { class: 'loading' }, 'Loading the notification outbox…'));
    let res;
    try {
      res = await listOutbox({ ...state.filters, limit: 200 });
    } catch (err) {
      clear(tableHost);
      errHost.appendChild(errorBox(err));
      return;
    }
    const strip = monitoringStrip(res.monitoring);
    if (strip) monitorHost.appendChild(strip);
    renderTable(res.items || []);
  }

  function renderTable(items) {
    clear(tableHost);
    if (!items.length) {
      tableHost.appendChild(h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '✉'),
        h('div', {}, 'No notification matches these filters.'),
      ]));
      return;
    }
    const thead = h('thead', {}, h('tr', {}, [
      'Event', 'Recipient', 'Subject', 'State', 'Attempts', 'Next attempt', 'Created',
    ].map((label) => h('th', { scope: 'col' }, label)).concat(
      h('th', { scope: 'col' }, [h('span', { class: 'sr-only' }, 'Actions')]),
    )));
    const tbody = h('tbody');
    items.forEach((n) => {
      const viewBtn = h('button', {
        type: 'button', class: 'btn-sm linkish', 'aria-label': `View notification ${n.notification_id}`,
        onClick: async () => { state.view = 'detail'; state.selectedId = n.notification_id; await renderDetail(root, n.notification_id); },
      }, 'View');
      tbody.appendChild(h('tr', {}, [
        h('th', { scope: 'row', class: 'mono' }, n.event),
        h('td', {}, n.recipient_user_id || n.recipient_email || '—'),
        h('td', {}, n.subject || '—'),
        h('td', {}, stateBadge(n.state)),
        h('td', { class: 'num mono' }, `${n.attempts}/${n.max_attempts}`),
        h('td', {}, n.next_attempt_at || '—'),
        h('td', {}, n.created_at || '—'),
        h('td', {}, viewBtn),
      ]));
    });
    tableHost.appendChild(h('table', {}, [
      h('caption', { class: 'sr-only' }, 'Notification outbox, filtered by state, event and recipient'),
      thead, tbody,
    ]));
  }

  load();
}

async function renderDetail(root, notificationId) {
  clear(root);
  const backBtn = h('button', {
    type: 'button', class: 'btn-sm',
    onClick: async () => { state.view = 'list'; state.selectedId = null; await renderList(root); },
  }, '← Back to outbox');
  const errHost = h('div');
  const body = h('div', { class: 'loading' }, 'Loading notification…');
  root.appendChild(h('div', { class: 'imr-screen' }, [backBtn, errHost, body]));

  let n;
  try {
    n = await getNotification(notificationId);
  } catch (err) {
    clear(body);
    errHost.appendChild(errorBox(err));
    return;
  }

  clear(body);
  const retryable = n.state === 'DEAD' || n.state === 'FAILED';
  const retryBtn = h('button', {
    type: 'button', class: 'btn-primary btn-sm', hidden: !retryable,
    onClick: async () => {
      retryBtn.disabled = true;
      try {
        await retryNotification(notificationId);
        announce('success', `${n.notification_id} re-queued.`);
        await renderDetail(root, notificationId);
      } catch (err) {
        clear(errHost);
        errHost.appendChild(errorBox(err));
      } finally {
        retryBtn.disabled = false;
      }
    },
  }, 'Retry');

  const deliveryRows = (n.deliveries || []).map((d) => h('tr', {}, [
    h('td', { class: 'num mono' }, String(d.attempt)),
    h('td', {}, d.at || '—'),
    h('td', {}, d.outcome || '—'),
    h('td', {}, d.provider || '—'),
    h('td', {}, d.detail || '—'),
  ]));

  body.appendChild(h('div', {}, [
    h('h2', {}, `${n.notification_id} — ${n.event}`),
    h('dl', { class: 'kv' }, [
      h('dt', {}, 'State'), h('dd', {}, stateBadge(n.state)),
      h('dt', {}, 'Recipient'), h('dd', {}, n.recipient_user_id || n.recipient_email || '—'),
      h('dt', {}, 'Subject'), h('dd', {}, n.subject || '—'),
      h('dt', {}, 'Template'), h('dd', { class: 'mono' }, n.template || '—'),
      h('dt', {}, 'Attempts'), h('dd', {}, `${n.attempts}/${n.max_attempts}`),
      h('dt', {}, 'Next attempt'), h('dd', {}, n.next_attempt_at || '—'),
      h('dt', {}, 'Last error'), h('dd', {}, n.last_error || '—'),
      h('dt', {}, 'Provider'), h('dd', {}, n.provider || '—'),
      h('dt', {}, 'Object'), h('dd', {}, `${n.object_type || '—'} ${n.object_id || ''}`.trim()),
      h('dt', {}, 'Correlation ID'), h('dd', { class: 'mono' }, n.correlation_id || '—'),
      h('dt', {}, 'Created'), h('dd', {}, n.created_at || '—'),
      h('dt', {}, 'Sent'), h('dd', {}, n.sent_at || '—'),
    ]),
    h('div', { class: 'btn-row' }, [retryBtn]),
    h('h3', {}, 'Body'),
    h('pre', { class: 'mono xs' }, n.body_text || '—'),
    h('h3', {}, 'Delivery attempts'),
    deliveryRows.length
      ? h('div', { class: 'table-wrap' }, h('table', {}, [
        h('caption', { class: 'sr-only' }, `Delivery attempts for ${n.notification_id}`),
        h('thead', {}, h('tr', {}, ['Attempt', 'At', 'Outcome', 'Provider', 'Detail'].map((l) => h('th', { scope: 'col' }, l)))),
        h('tbody', {}, deliveryRows),
      ]))
      : h('p', { class: 'muted' }, 'No delivery attempt has been made yet.'),
  ]));
}

export async function mountNotificationsAdmin(root) {
  if (!root) return;
  if (state.view === 'detail' && state.selectedId) await renderDetail(root, state.selectedId);
  else await renderList(root);
}
