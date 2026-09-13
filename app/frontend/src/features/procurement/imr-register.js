/* app/frontend/src/features/procurement/imr-register.js
   Internal Material Requests (migration 034) — register and detail, the
   frontend for app/backend/api/internal_fulfilment.py's IMR half (the
   fulfilment DECISION on a PR line is the other half, on the Purchase
   Requests screen in app.js itself).

   Modelled on purchase-order.js: an ES module exporting one mount function,
   registered in app.js as a NAV-listed view (V.imrs) rather than through
   src/core/router.js's SCREENS table, because the brief for this screen
   asked for a plain NAV row. Money is rendered through the shell's own
   paise formatter (window.inr, app.js's inr()) and never computed here;
   quantities are the decimal STRINGS the server returns, never parsed to a
   float for display.

   Register <-> detail navigation is handled INSIDE this module (no
   S.view change, no hash change) so the two screens can be told apart
   without a second route. Every ACTION (approve, valuation, allocate,
   issue, return, consume, transfer, cancel) opens the shell's own
   window.dialog() — the same focus-trapped, Escape-closes dialog every
   other screen in this application uses — because a feature module cannot
   usefully reinvent that machinery: dialog() lives once, in app.js, and is
   a classic-script global precisely so an ES module screen can call it.
   Its onOk wrapper calls the shell's render() on success, which re-invokes
   V.imrs() and therefore mountImrs() again; the module-level `state` below
   is what lets the re-mount land back on the same register filters or the
   same open detail, since a brand-new host <div> is built on every render()
   and carries no memory of its own.
*/

import { h, text, clear } from '../../core/dom.js';
import { parseRupeesToPaise } from '../../components/budget/money-input.js';
import {
  getFulfilment, listImrs, getImr, approveImr, setValuation, allocateImr,
  issueImr, returnImr, consumeImr, transferImr, cancelImr, problemDetail,
  InternalFulfilmentApiError,
} from './internal-fulfilment-api.js';

const TERMINAL = new Set(['CONSUMED', 'RETURNED', 'CANCELLED']);

const STATUS_ROLE = {
  REQUESTED: 'neutral', APPROVED: 'progress', ALLOCATED: 'progress',
  PARTIALLY_ISSUED: 'progress', ISSUED: 'progress',
  PARTIALLY_CONSUMED: 'progress', CONSUMED: 'positive',
  RETURNED: 'neutral', CANCELLED: 'negative',
};
const SYM = { positive: '✔', negative: '✖', warning: '!', progress: '◐', neutral: '·' };

/* app.js's own globals (dialog, msg, flash, esc, inr, can, approvingAs, S) are
   classic-script top-level declarations, which land on `window` — the one
   deliberate seam an ES-module screen uses to reach the shell's shared
   widgets, the same way procurement-api.js::getShellBootstrap and
   budget-api.js::getPrincipal already read `S` off it. Guarded so this
   module never hard-crashes if it is ever mounted standalone. */
function shell() { return (typeof window !== 'undefined' ? window : {}); }
function can(permission) { const w = shell(); return typeof w.can === 'function' && w.can(permission); }
function fmtMoney(paise) { const w = shell(); return typeof w.inr === 'function' ? w.inr(paise) : String(paise ?? '—'); }
function escText(s) { const w = shell(); return typeof w.esc === 'function' ? w.esc(s) : String(s ?? ''); }
function msgHtml(kind, body) { const w = shell(); return typeof w.msg === 'function' ? w.msg(kind, body) : `<div>${body}</div>`; }
function openDialog(title, bodyHtml, onOk, okLabel, opts) {
  const w = shell();
  if (typeof w.dialog !== 'function') { throw new Error('The shared dialog is not available on this page.'); }
  w.dialog(title, bodyHtml, onOk, okLabel, opts);
}
function announce(kind, message) { const w = shell(); if (typeof w.flash === 'function') w.flash(kind, message); }
function approvingAsHtml(extra) {
  const w = shell();
  if (typeof w.approvingAs === 'function') return w.approvingAs(extra);
  return msgHtml('info', extra || '');
}
function newIdempotencyKey() {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  } catch { /* fall through */ }
  return `imr-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/* Module-level: survives a shell render() re-mounting this screen (see the
   file banner), does not survive a page reload -- which is fine, a reload
   returning to the register rather than a stale detail is the safer
   default. */
const state = {
  view: 'list',
  selectedImrId: null,
  filters: { projectId: '', status: '' },
};

function statusBadge(s) {
  const role = STATUS_ROLE[s] || 'neutral';
  return h('span', { class: `status st-${role}` }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, SYM[role] || '·'),
    text(s || '—'),
  ]);
}

function errorBox(err) {
  const detail = err instanceof InternalFulfilmentApiError
    ? problemDetail(err)
    : { code: null, message: (err && err.message) || 'This request could not be completed.' };
  return h('div', { class: 'msg msg-error', role: 'alert' }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
    h('div', { class: 'body' }, [
      detail.code ? h('div', {}, h('span', { class: 'mono imr-code' }, detail.code)) : null,
      h('div', {}, detail.message),
    ].filter(Boolean)),
  ]);
}

function field(labelText, id, el) {
  return h('div', { class: 'field' }, [h('label', { for: id }, labelText), el]);
}

function actionsFor(imr) {
  const status = imr.status;
  return {
    approve: can('imr.approve') && status === 'REQUESTED',
    valuation: can('imr.allocate') && (status === 'REQUESTED' || status === 'APPROVED'),
    allocate: can('imr.allocate') && (status === 'APPROVED' || status === 'ALLOCATED'),
    issue: can('imr.issue') && ['ALLOCATED', 'PARTIALLY_ISSUED', 'PARTIALLY_CONSUMED'].includes(status),
    returnMaterial: can('imr.issue') && ['PARTIALLY_ISSUED', 'ISSUED', 'PARTIALLY_CONSUMED'].includes(status),
    consume: can('imr.issue') && ['PARTIALLY_ISSUED', 'ISSUED', 'PARTIALLY_CONSUMED'].includes(status),
    transfer: can('imr.issue') && ['ALLOCATED', 'PARTIALLY_ISSUED', 'PARTIALLY_CONSUMED'].includes(status),
    cancel: can('imr.cancel') && !TERMINAL.has(status),
  };
}

/* ============================================================ the register */

async function renderList(root) {
  clear(root);
  const toolbar = h('div', { class: 'toolbar' });
  const errHost = h('div', { class: 'imr-error-host' });
  const tableHost = h('div', { class: 'table-wrap' });
  root.appendChild(h('div', { class: 'imr-screen' }, [toolbar, errHost, tableHost]));

  const w = shell();
  const projects = (w.S && w.S.boot && Array.isArray(w.S.boot.projects)) ? w.S.boot.projects : [];
  const projectSelect = h('select', { id: 'imrFilterProject' }, [
    h('option', { value: '' }, 'All projects'),
    ...projects.map((p) => h('option', { value: p.project_id, selected: p.project_id === state.filters.projectId },
      `${p.capex_code || p.project_id} — ${p.name || ''}`)),
  ]);
  const statusSelect = h('select', { id: 'imrFilterStatus' }, [
    h('option', { value: '' }, 'All statuses'),
  ]);
  const refreshBtn = h('button', { type: 'button', class: 'btn-sm', id: 'imrRefresh' }, 'Apply filters');
  toolbar.appendChild(field('Project', 'imrFilterProject', projectSelect));
  toolbar.appendChild(field('Status', 'imrFilterStatus', statusSelect));
  toolbar.appendChild(h('div', { class: 'field field-action' }, refreshBtn));

  async function load() {
    clear(errHost);
    tableHost.appendChild(h('div', { class: 'loading' }, 'Loading internal material requests…'));
    let res;
    try {
      res = await listImrs({
        projectId: projectSelect.value || undefined,
        status: statusSelect.value || undefined,
        limit: 200,
      });
    } catch (err) {
      clear(tableHost);
      errHost.appendChild(errorBox(err));
      return;
    }
    clear(statusSelect);
    statusSelect.appendChild(h('option', { value: '' }, 'All statuses'));
    (res.statuses || []).forEach((s) => statusSelect.appendChild(
      h('option', { value: s, selected: s === state.filters.status }, s)));
    renderTable(res.items || []);
  }

  function renderTable(items) {
    clear(tableHost);
    if (!items.length) {
      tableHost.appendChild(h('div', { class: 'card-body' }, [
        h('div', { class: 'empty' }, [
          h('div', { class: 'big', 'aria-hidden': 'true' }, '⇆'),
          h('div', {}, 'No internal material request matches these filters.'),
        ]),
      ]));
      return;
    }
    const headCells = [
      'Number', 'PR', 'WBS', 'Item', 'Req.', 'Appr.', 'Alloc.', 'Issued', 'Returned', 'Consumed',
      'Valuation', 'Allocation ₹', 'Consumption ₹', 'Status', 'Mapping',
    ].map((label, i) => h('th', { scope: 'col', class: (i >= 4 && i <= 9) || i === 11 || i === 12 ? 'num' : '' }, label));
    headCells.push(h('th', { scope: 'col' }, [h('span', { class: 'sr-only' }, 'Actions')]));
    const thead = h('thead', {}, h('tr', {}, headCells));
    const tbody = h('tbody');
    items.forEach((imr) => {
      const viewBtn = h('button', {
        type: 'button',
        class: 'btn-sm linkish',
        'aria-label': `View ${imr.imr_number}`,
        onClick: async () => {
          state.view = 'detail';
          state.selectedImrId = imr.imr_id;
          await renderDetail(root, imr.imr_id);
        },
      }, 'View');
      tbody.appendChild(h('tr', {}, [
        h('th', { scope: 'row', class: 'mono' }, imr.imr_number),
        h('td', { class: 'mono' }, imr.pr_number || '—'),
        h('td', { class: 'mono' }, imr.wbs_code || '—'),
        h('td', {}, imr.item_description || imr.item_external_id || '—'),
        h('td', { class: 'num mono' }, String(imr.requested_quantity ?? '—')),
        h('td', { class: 'num mono' }, String(imr.approved_quantity ?? '—')),
        h('td', { class: 'num mono' }, String(imr.allocated_quantity ?? '—')),
        h('td', { class: 'num mono' }, String(imr.issued_quantity ?? '—')),
        h('td', { class: 'num mono' }, String(imr.returned_quantity ?? '—')),
        h('td', { class: 'num mono' }, String(imr.consumed_quantity ?? '—')),
        h('td', {}, [
          text(imr.valuation_source || 'MISSING'),
          imr.unit_rate_paise !== null && imr.unit_rate_paise !== undefined
            ? h('div', { class: 'xs muted' }, `@ ${fmtMoney(imr.unit_rate_paise)}/unit`)
            : null,
        ].filter(Boolean)),
        h('td', { class: 'num' }, fmtMoney(imr.internal_allocation_paise)),
        h('td', { class: 'num' }, fmtMoney(imr.internal_consumption_paise)),
        h('td', {}, statusBadge(imr.status)),
        h('td', {}, imr.mapping_ok
          ? h('span', { class: 'status st-positive' }, [h('span', { class: 'sym', 'aria-hidden': 'true' }, '✔'), text('Mapped')])
          : h('span', { class: 'status st-negative' }, [h('span', { class: 'sym', 'aria-hidden': 'true' }, '✖'), text('Unmapped')])),
        h('td', {}, viewBtn),
      ]));
    });
    const table = h('table', {}, [
      h('caption', { class: 'sr-only' }, 'Internal material requests with their quantities, valuation, internal allocation and consumption, status and mapping'),
      thead, tbody,
    ]);
    tableHost.appendChild(table);
  }

  projectSelect.addEventListener('change', () => { state.filters.projectId = projectSelect.value; load(); });
  statusSelect.addEventListener('change', () => { state.filters.status = statusSelect.value; load(); });
  refreshBtn.addEventListener('click', () => load());

  await load();
}

/* ============================================================== the detail */

function kv(pairs) {
  return h('div', { class: 'kv' }, pairs.flatMap(([k, v]) => [h('dt', {}, k), h('dd', {}, v)]));
}

function moneyTiles(money) {
  const tile = (label, paise, accent) => h('div', { class: `tile accent-${accent || 'info'}` }, [
    h('div', { class: 'k' }, label),
    h('div', { class: 'v' }, fmtMoney(paise)),
  ]);
  return h('div', { class: 'tiles' }, [
    tile('Internal allocation (open)', money.internal_allocation_paise, 'watch'),
    tile('Internal consumption (CWIP)', money.internal_consumption_paise, 'safe'),
    tile('Allocated (lifetime)', money.allocated_paise),
    tile('Issued (lifetime)', money.issued_paise),
    tile('Returned', money.returned_paise),
    tile('Cancelled', money.cancelled_paise),
  ]);
}

function movementsTable(movements) {
  if (!movements.length) return h('p', { class: 'muted' }, 'No stock movement has been recorded against this request.');
  const thead = h('thead', {}, h('tr', {}, [
    'Kind', 'Quantity', 'Amount', 'From', 'To', 'Reference', 'Note', 'Occurred', 'By',
  ].map((l, i) => h('th', { scope: 'col', class: i === 1 || i === 2 ? 'num' : '' }, l))));
  const tbody = h('tbody', {}, movements.map((m) => h('tr', {}, [
    h('th', { scope: 'row' }, m.kind),
    h('td', { class: 'num mono' }, String(m.quantity)),
    h('td', { class: 'num' }, fmtMoney(m.amount_paise)),
    h('td', { class: 'mono' }, m.from_location_id || '—'),
    h('td', { class: 'mono' }, m.to_location_id || '—'),
    h('td', {}, m.reference || '—'),
    h('td', {}, m.note || '—'),
    h('td', {}, m.occurred_at || '—'),
    h('td', {}, m.created_by || '—'),
  ])));
  return h('table', {}, [
    h('caption', { class: 'sr-only' }, 'Stock movements against this internal material request'),
    thead, tbody,
  ]);
}

function exceptionsList(exceptions) {
  if (!exceptions.length) return h('p', { class: 'muted' }, 'No exception is open or has been raised against this request.');
  return h('ul', { class: 'imr-exceptions' }, exceptions.map((e) => h('li', {}, [
    h('span', { class: `status ${e.status === 'Open' ? 'st-warning' : 'st-neutral'}` }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, e.status === 'Open' ? '!' : '·'),
      text(`${e.kind} — ${e.status}`),
    ]),
    h('div', {}, e.detail || ''),
    h('div', { class: 'xs muted' }, e.raised_at || ''),
  ])));
}

async function renderDetail(root, imrId) {
  clear(root);
  const backBtn = h('button', {
    type: 'button',
    class: 'btn-sm',
    onClick: async () => { state.view = 'list'; state.selectedImrId = null; await renderList(root); },
  }, '← Back to register');
  const errHost = h('div', { class: 'imr-error-host' });
  const body = h('div', { class: 'loading' }, 'Loading request…');
  root.appendChild(h('div', { class: 'imr-screen' }, [backBtn, errHost, body]));

  let imr;
  try {
    imr = await getImr(imrId);
  } catch (err) {
    clear(body);
    errHost.appendChild(errorBox(err));
    return;
  }

  async function reload() {
    try {
      imr = await getImr(imrId);
    } catch (err) {
      clear(errHost);
      errHost.appendChild(errorBox(err));
      return;
    }
    paint();
  }

  function actionDialog(title, bodyHtml, run, okLabel) {
    openDialog(title, bodyHtml, async () => {
      await run();
      await reload();
    }, okLabel);
  }

  function wireApprove() {
    return h('button', { type: 'button', class: 'btn-sm', onClick: () => actionDialog(
      `Approve ${imr.imr_number}`,
      approvingAsHtml(`You are approving ${escText(imr.imr_number)}. The requester never approves their own request (403 SELF_APPROVAL).`)
      + `<div class="field"><label for="imrApproveQty">Approved quantity (blank = the requested quantity, ${escText(imr.requested_quantity)})</label>
           <input id="imrApproveQty" class="num" type="text" inputmode="decimal"></div>
         <div class="field"><label for="imrApproveReason">Reason (optional)</label><input id="imrApproveReason" maxlength="2000"></div>
         <div class="field"><label for="imrApproveActingFor">Acting for user id (only for a registered delegation)</label><input id="imrApproveActingFor" maxlength="100"></div>`,
      async () => {
        const qty = document.getElementById('imrApproveQty').value.trim();
        const reason = document.getElementById('imrApproveReason').value.trim();
        const actingFor = document.getElementById('imrApproveActingFor').value.trim();
        await approveImr(imrId, {
          approved_quantity: qty || undefined,
          reason: reason || undefined,
          acting_for_user_id: actingFor || undefined,
          version_no: imr.version_no,
        });
        announce('success', `${imr.imr_number} approved.`);
      }, 'Approve') }, 'Approve');
  }

  function wireValuation() {
    return h('button', { type: 'button', class: 'btn-sm', onClick: () => actionDialog(
      `Set valuation — ${imr.imr_number}`,
      msgHtml('info', 'A manual valuation is a reasoned rate override and freezes at allocation (409 VALUATION_FROZEN afterwards).')
      + `<div class="field"><label for="imrRate">Unit rate (₹) *</label><input id="imrRate" class="num" type="text" inputmode="decimal" required></div>
         <div class="field"><label for="imrValNote">Note *</label><input id="imrValNote" maxlength="2000" required></div>
         <div class="field"><label for="imrValRef">Reference (optional)</label><input id="imrValRef" maxlength="200"></div>`,
      async () => {
        const rateRaw = document.getElementById('imrRate').value.trim();
        const note = document.getElementById('imrValNote').value.trim();
        const reference = document.getElementById('imrValRef').value.trim();
        if (!note) throw new Error('A valuation note is required.');
        const unitRatePaise = parseRupeesToPaise(rateRaw);
        await setValuation(imrId, {
          unit_rate_paise: unitRatePaise, note, reference: reference || undefined,
          version_no: imr.version_no,
        });
        announce('success', `${imr.imr_number} valued at ${fmtMoney(unitRatePaise)}/unit.`);
      }, 'Save valuation') }, 'Set valuation');
  }

  function wireAllocate() {
    return h('button', { type: 'button', class: 'btn-sm btn-primary', onClick: () => actionDialog(
      `Allocate — ${imr.imr_number}`,
      msgHtml('info', 'Sets the approved quantity aside for the project. Refused with INTERNAL_VALUATION_MISSING if no rate is on file, and with BUDGET_EXCEEDED if availability has moved.'),
      async () => {
        const result = await allocateImr(imrId, { idempotency_key: newIdempotencyKey(), version_no: imr.version_no });
        announce('success', `${imr.imr_number} allocated. Covered by hold: ${fmtMoney(result.covered_by_hold_paise)}; newly exposed: ${fmtMoney(result.newly_exposed_paise)}.`);
      }, 'Allocate') }, 'Allocate');
  }

  function quantityDialog(title, hint, run, okLabel, { withLocation = false } = {}) {
    return actionDialog(title,
      msgHtml('info', hint)
      + `<div class="field"><label for="imrMoveQty">Quantity *</label><input id="imrMoveQty" class="num" type="text" inputmode="decimal" required></div>`
      + (withLocation ? `<div class="field"><label for="imrMoveToLoc">To store (location id)${withLocation === 'required' ? ' *' : ' (optional)'}</label><input id="imrMoveToLoc" maxlength="100"></div>` : '')
      + `<div class="field"><label for="imrMoveRef">Reference (optional)</label><input id="imrMoveRef" maxlength="200"></div>
         <div class="field"><label for="imrMoveNote">Note (optional)</label><input id="imrMoveNote" maxlength="2000"></div>`,
      async () => {
        const quantity = document.getElementById('imrMoveQty').value.trim();
        if (!quantity) throw new Error('A quantity is required.');
        const toLocation = withLocation ? document.getElementById('imrMoveToLoc').value.trim() : '';
        if (withLocation === 'required' && !toLocation) throw new Error('A destination store is required for a transfer.');
        const reference = document.getElementById('imrMoveRef').value.trim();
        const note = document.getElementById('imrMoveNote').value.trim();
        await run({
          quantity, idempotency_key: newIdempotencyKey(), version_no: imr.version_no,
          to_location_id: toLocation || undefined, reference: reference || undefined, note: note || undefined,
        });
      }, okLabel);
  }

  function wireIssue() {
    return h('button', { type: 'button', class: 'btn-sm', onClick: () => quantityDialog(
      `Issue — ${imr.imr_number}`,
      'Stock leaves the store for the project; allocation becomes CWIP (422 ISSUE_EXCEEDS_ALLOCATION if the quantity is too large).',
      async (body) => { await issueImr(imrId, body); announce('success', `${imr.imr_number}: stock issued.`); },
      'Issue', { withLocation: true }) }, 'Issue');
  }
  function wireReturn() {
    return h('button', { type: 'button', class: 'btn-sm', onClick: () => quantityDialog(
      `Return — ${imr.imr_number}`,
      'Issued, unconsumed stock goes back to the store (422 RETURN_EXCEEDS_ISSUED if the quantity is too large).',
      async (body) => { await returnImr(imrId, body); announce('success', `${imr.imr_number}: stock returned.`); },
      'Return') }, 'Return');
  }
  function wireConsume() {
    return h('button', { type: 'button', class: 'btn-sm', onClick: () => quantityDialog(
      `Consume — ${imr.imr_number}`,
      'The site confirms use of issued stock. No money moves here — it was booked at issue (422 CONSUME_EXCEEDS_ISSUED if the quantity is too large).',
      async (body) => { await consumeImr(imrId, body); announce('success', `${imr.imr_number}: consumption recorded.`); },
      'Consume') }, 'Consume');
  }
  function wireTransfer() {
    return h('button', { type: 'button', class: 'btn-sm', onClick: () => quantityDialog(
      `Transfer — ${imr.imr_number}`,
      'Allocated, unissued stock moves between stores. No money moves (422 TRANSFER_EXCEEDS_ALLOCATION, 422 TRANSFER_SAME_STORE).',
      async (body) => { await transferImr(imrId, body); announce('success', `${imr.imr_number}: transferred.`); },
      'Transfer', { withLocation: 'required' }) }, 'Transfer');
  }
  function wireCancel() {
    return h('button', { type: 'button', class: 'btn-sm btn-danger', onClick: () => actionDialog(
      `Cancel — ${imr.imr_number}`,
      msgHtml('warning', 'Whatever is still allocated is released. Refused (409 STOCK_IN_THE_FIELD) if stock is issued and not yet returned, or already consumed.')
      + `<div class="field"><label for="imrCancelReason">Reason *</label><input id="imrCancelReason" maxlength="2000" required></div>`,
      async () => {
        const reason = document.getElementById('imrCancelReason').value.trim();
        if (!reason) throw new Error('A cancellation carries its reason.');
        await cancelImr(imrId, { reason, version_no: imr.version_no });
        announce('success', `${imr.imr_number} cancelled.`);
      }, 'Cancel request') }, 'Cancel');
  }

  function paint() {
    clear(body);
    const acts = actionsFor(imr);
    const buttons = [
      acts.approve ? wireApprove() : null,
      acts.valuation ? wireValuation() : null,
      acts.allocate ? wireAllocate() : null,
      acts.issue ? wireIssue() : null,
      acts.returnMaterial ? wireReturn() : null,
      acts.consume ? wireConsume() : null,
      acts.transfer ? wireTransfer() : null,
      acts.cancel ? wireCancel() : null,
    ].filter(Boolean);

    body.appendChild(h('div', {}, [
      h('h3', { class: 'section-title' }, [
        `${imr.imr_number} `, statusBadge(imr.status),
        imr.mapping_ok ? null : h('span', { class: 'status st-negative' }, [h('span', { class: 'sym', 'aria-hidden': 'true' }, '✖'), text('Unmapped')]),
      ].filter(Boolean)),
      kv([
        ['Purchase request', imr.pr_number || '—'],
        ['WBS', `${imr.wbs_code || imr.wbs_id || '—'}`],
        ['Budget head', imr.budget_head_id || '—'],
        ['Item', imr.item_description || imr.item_external_id || '—'],
        ['From store', imr.from_location_id || '—'],
        ['To store', imr.to_location_id || '—'],
        ['Requested by', imr.requested_by || '—'],
        ['Approved by', imr.approved_by || '—'],
        ['Reason', imr.reason || '—'],
        ['Version', String(imr.version_no ?? '—')],
      ]),
      h('div', { class: 'card' }, [
        h('h3', {}, 'Money — internal allocation and consumption, kept separate from external commitment and actual'),
        moneyTiles(imr),
      ]),
      buttons.length
        ? h('div', { class: 'imr-actions' }, buttons)
        : h('p', { class: 'muted' }, 'No action on this request is available to your role at its current status.'),
      h('div', { class: 'card' }, [h('h3', {}, 'Stock movements'), h('div', { class: 'table-wrap' }, movementsTable(imr.movements || []))]),
      h('div', { class: 'card' }, [h('h3', {}, 'Exceptions'), exceptionsList(imr.exceptions || [])]),
    ]));
  }

  paint();
}

/* ============================================================== entry point */

export async function mountImrs(root) {
  if (!root) return;
  if (state.view === 'detail' && state.selectedImrId) {
    await renderDetail(root, state.selectedImrId);
  } else {
    await renderList(root);
  }
}
