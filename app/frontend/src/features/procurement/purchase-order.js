/* app/frontend/src/features/procurement/purchase-order.js
   "Raise Purchase Order" -- the screen for POST /api/procurement/purchase-
   orders (Fable 5.1, migration 029), which shipped with no screen at all:
   the Commitments view lists, amends, cancels and closes orders, and the
   API can originate one in the vendor's own currency, but nothing in the
   running application could raise one.

   What the currency does here
   ---------------------------
   The order's currency is a HEADER attribute, chosen once from the rate
   book's currencies plus INR (the default). On INR the lines carry rupees
   and paise, exactly the base-currency payload `_PurchaseOrderIn` has
   always taken, with NO currency field at all. On any other currency:

     * each line's amount is entered in THAT currency's minor units at its
       own exponent -- a yen amount is a whole number of yen, a dinar has
       three places -- through components/budget/money-input.js at the
       exponent (JPY 0, INR/USD 2, KWD 3);
     * the rate is the ACTIVE one on file for the DOCUMENT DATE, resolved by
       GET /api/fx/rates/lookup -- the same answer `fx.resolve_basis` gives
       at the write -- and shown with its source and rate-book entry. When
       the lookup refuses (FX_RATE_UNAVAILABLE, FX_RATE_AMBIGUOUS) the code
       is shown VERBATIM, submit is disabled, and the Exchange Rates screen
       is linked, because the fix is to record the rate, not to type one;
     * the INR figure shown is fx-translate.js's mirror of the server's
       arithmetic (translate the summed source once, half away from zero,
       allocate by largest remainder), so the preview is the number the
       ledger will hold. The response document's `amount_paise` is what is
       shown after the write.

   Nothing in this file multiplies money as a float, prints an object at a
   user, or emits a `style=` attribute: every element is built through
   core/dom.js's h(), which refuses one.
*/

import { h, text, clear } from '../../core/dom.js';
import { parseAmountToMinor } from '../../components/budget/money-input.js';
import { listRates, lookupRate, FxApiError } from '../settings/fx-api.js';
import { formatSettingsDate } from '../settings/format.js';
import {
  createPurchaseOrder, getShellBootstrap, getProcurableWbs, problemDetail, ProcurementApiError,
} from './procurement-api.js';
import { translateDocument, formatMinor, formatPaise } from './fx-translate.js';

export const BASE_CURRENCY = 'INR';

/* The minor-unit exponents the server seeds (app/backend/money.py::
   MINOR_EXPONENTS, mirrored by migration 023's currency_denomination). This
   table decides how an amount is PARSED before the rate lookup has answered;
   the lookup's `source_minor_exponent` -- read from the database -- is the
   authority and replaces it the moment it arrives. A currency the table does
   not know parses at two places until then, which is the same default
   `money.minor_exponent_of` applies at the parsing boundary. */
export const CURRENCY_MINOR_EXPONENT = {
  INR: 2, USD: 2, EUR: 2, GBP: 2, AED: 2, SGD: 2, CHF: 2, AUD: 2, CNY: 2, JPY: 0, KWD: 3,
};

export function minorExponentOf(code) {
  const exp = CURRENCY_MINOR_EXPONENT[String(code || '').toUpperCase()];
  return Number.isInteger(exp) ? exp : 2;
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function msgBox(kind, body, { messageId, alert = false, actions = [] } = {}) {
  const icon = { error: '✖', warning: '!', success: '✔', info: '·' }[kind] || '·';
  const attrs = { class: `msg msg-${kind}` };
  if (alert) attrs.role = 'alert';
  return h('div', attrs, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, icon),
    h('div', { class: 'body' }, [
      body,
      actions.length ? h('div', { class: 'actions' }, actions) : null,
      messageId ? h('div', { class: 'mid' }, messageId) : null,
    ].filter(Boolean)),
  ]);
}

/**
 * The `_PurchaseOrderIn` document for one header and its parsed lines.
 * Pure, so the spec (and a reader) can see exactly what leaves the browser.
 *
 * @param {{projectId: string, vendorName: string, poNumber: string,
 *          currency: string, documentDate: string,
 *          rate: null|{rate: string, rate_source: string, fx_rate_id: string}}} header
 * @param {Array<{wbsId: string, headId: string, description: string,
 *          quantity: number, amountMinor: number}>} lines - already parsed at
 *          the currency's exponent.
 * @returns {Object}
 */
export function buildPurchaseOrderPayload(header, lines) {
  const foreign = header.currency !== BASE_CURRENCY;
  const payload = {
    project_id: header.projectId,
    vendor_name: header.vendorName,
    lines: lines.map((line) => {
      const out = { wbs_id: line.wbsId, budget_head_id: line.headId, quantity: line.quantity };
      if (line.description) out.description = line.description;
      // ONE line shape per document, and it is the header's: base paise on
      // an INR order, source minor units on a foreign one -- never both.
      if (foreign) out.source_amount_minor = line.amountMinor;
      else out.amount_paise = line.amountMinor;
      return out;
    }),
  };
  if (header.poNumber) payload.po_number = header.poNumber;
  if (foreign) {
    payload.currency = header.currency;
    payload.document_date = header.documentDate;
    // The rate the rate book answered for the document date, named three
    // ways so the write cites the SAME row: `fx_rate_id` is what
    // fx.resolve_basis reads; `exchange_rate` (the exact decimal string,
    // never a Number) and `rate_source` say in the request what that row
    // holds, so the payload is legible on its own and any drift between
    // them is the server's to refuse.
    payload.exchange_rate = header.rate.rate;
    payload.rate_source = header.rate.rate_source;
    payload.fx_rate_id = header.rate.fx_rate_id;
  }
  return payload;
}

export function mountPurchaseOrder(root) {
  if (!root) return;
  const liveRegion = document.getElementById('poLiveRegion');
  function announce(message) { if (liveRegion) liveRegion.textContent = message; }

  const state = {
    permissions: new Set(),
    projects: [],
    budgetHeads: [],
    wbs: [],                 // procurable nodes for the selected project
    currencies: [BASE_CURRENCY],
    rateBookState: 'loading', // loading|ready|error
    currency: BASE_CURRENCY,
    exponent: 2,
    documentDate: todayIso(),
    rate: null,              // the lookup answer, or null
    rateError: null,         // {code, message} when the lookup refused
    lookupSeq: 0,
    lines: [],
    nextLineId: 1,
    submitting: false,
  };
  function can(permission) { return state.permissions.has(permission); }
  function isForeign() { return state.currency !== BASE_CURRENCY; }

  /* ---------------- hosts ---------------- */
  const errorHost = h('div', { id: 'poErrorHost', class: 'po-error-host' });
  const resultHost = h('div', { id: 'poResultHost', class: 'po-result-host' });

  function showError(err) {
    clear(errorHost);
    const detail = (err instanceof ProcurementApiError || err instanceof FxApiError)
      ? problemDetail(err)
      : { code: null, message: (err && err.message) || 'This request could not be completed.', messageId: null, extra: null };
    errorHost.appendChild(msgBox('error', h('div', {}, [
      detail.code ? h('div', {}, [h('span', { class: 'mono po-code' }, detail.code)]) : null,
      h('div', {}, detail.message),
    ].filter(Boolean)), { alert: true, messageId: detail.messageId }));
    announce(detail.code ? `Refused: ${detail.code}.` : detail.message);
  }
  function clearError() { clear(errorHost); }

  /* ---------------- header ---------------- */
  const projectSelect = h('select', { id: 'poProject', required: true });
  const vendorInput = h('input', { type: 'text', id: 'poVendor', required: true, maxlength: '200', autocomplete: 'organization', placeholder: 'As the vendor is named on the quotation' });
  const numberInput = h('input', { type: 'text', id: 'poNumber', maxlength: '40', autocomplete: 'off', placeholder: 'Leave blank to number automatically' });
  const dateInput = h('input', { type: 'date', id: 'poDate', required: true, value: state.documentDate });
  const currencySelect = h('select', { id: 'poCurrency', required: true, 'aria-describedby': 'poCurrencyHint' });
  const currencyHint = h('div', { id: 'poCurrencyHint', class: 'field-hint xs' }, 'Rate book loading…');
  const fieldErrs = {};
  function fieldErr(id) { fieldErrs[id] = h('div', { class: 'field-err xs st-negative', hidden: true }); return fieldErrs[id]; }
  function showFieldErr(id, message) { const el = fieldErrs[id]; if (!el) return; el.hidden = false; clear(el); el.appendChild(text(message)); }
  function clearFieldErrs() { Object.values(fieldErrs).forEach((el) => { el.hidden = true; clear(el); }); }

  const headerForm = h('div', { class: 'toolbar po-header', role: 'group', 'aria-labelledby': 'poHeaderTitle' }, [
    h('div', { class: 'field' }, [h('label', { for: 'poProject' }, 'Project *'), projectSelect]),
    h('div', { class: 'field f2' }, [h('label', { for: 'poVendor' }, 'Vendor *'), vendorInput, fieldErr('vendor')]),
    h('div', { class: 'field' }, [h('label', { for: 'poNumber' }, 'PO number'), numberInput]),
    h('div', { class: 'field' }, [h('label', { for: 'poDate' }, 'Document date *'), dateInput, fieldErr('date')]),
    h('div', { class: 'field' }, [h('label', { for: 'poCurrency' }, 'Currency *'), currencySelect, currencyHint]),
  ]);

  /* ---------------- rate panel ---------------- */
  const ratePanel = h('div', { id: 'poRatePanel', class: 'po-rate-panel', 'aria-live': 'polite', hidden: true });

  function renderRatePanel() {
    clear(ratePanel);
    if (!isForeign()) { ratePanel.hidden = true; return; }
    ratePanel.hidden = false;
    if (state.rateError) {
      const { code, message } = state.rateError;
      ratePanel.appendChild(msgBox('error', h('div', {}, [
        code ? h('div', {}, [h('span', { class: 'mono po-code' }, code)]) : null,
        h('div', {}, message),
        h('div', { class: 'xs' }, [
          'A document in this currency is refused rather than translated at another day\'s rate or at face value. Record and activate the rate for this date on ',
          h('a', { href: '#fx-rates', class: 'po-fx-link' }, 'Exchange Rates'),
          ', then choose the currency again.',
        ]),
      ].filter(Boolean)), { alert: true }));
      return;
    }
    if (!state.rate) {
      ratePanel.appendChild(msgBox('info', h('div', {}, `Looking up the ${state.currency}/${BASE_CURRENCY} rate for ${formatSettingsDate(state.documentDate)}…`)));
      return;
    }
    const r = state.rate;
    ratePanel.appendChild(msgBox('success', h('div', {}, [
      h('div', {}, [
        h('strong', {}, `${r.source}/${r.target} on ${formatSettingsDate(r.date)}: `),
        h('span', { class: 'mono po-rate', id: 'poRateValue' }, r.rate),
      ]),
      h('div', { class: 'xs' }, [
        'Source ', h('span', { class: 'mono', id: 'poRateSource' }, r.rate_source),
        ' · Rate book entry ', h('span', { class: 'mono', id: 'poRateId' }, r.fx_rate_id),
        ` · Minor-unit exponent ${r.source} ${r.source_minor_exponent}`,
      ]),
      h('div', { class: 'xs' }, 'Every line is entered in the order\'s currency; the INR column is what this rate makes of it, applied once to the document total and allocated across the lines to the paisa.'),
    ])));
  }

  /* ---------------- lines ---------------- */
  const amountHeader = h('th', { scope: 'col', class: 'num', id: 'poAmountHeader' }, 'Amount (INR)');
  const linesBody = h('tbody', { id: 'poLinesBody' });
  const totalSource = h('td', { class: 'num mono', id: 'poTotalSource' }, '—');
  const totalInr = h('td', { class: 'num mono', id: 'poTotalInr' }, '—');
  const linesTable = h('table', { id: 'poLines', class: 'po-lines' }, [
    h('caption', { class: 'sr-only' }, 'Purchase order lines: WBS element, budget head, description, quantity, amount in the order\'s currency and the INR figure'),
    h('thead', {}, [h('tr', {}, [
      h('th', { scope: 'col' }, 'WBS element'),
      h('th', { scope: 'col' }, 'Budget head'),
      h('th', { scope: 'col' }, 'Description'),
      h('th', { scope: 'col', class: 'num' }, 'Qty'),
      amountHeader,
      h('th', { scope: 'col', class: 'num' }, 'INR'),
      h('th', { scope: 'col' }, [h('span', { class: 'sr-only' }, 'Remove')]),
    ])]),
    linesBody,
    h('tfoot', {}, [h('tr', {}, [
      h('th', { scope: 'row', colspan: '4' }, 'Total'),
      totalSource, totalInr, h('td', {}),
    ])]),
  ]);

  function amountLabel() {
    return isForeign()
      ? `Amount (${state.currency}, ${state.exponent === 0 ? 'whole units' : `${state.exponent} decimal${state.exponent === 1 ? '' : 's'}`})`
      : 'Amount (INR)';
  }

  function newLine() {
    const id = state.nextLineId;
    state.nextLineId += 1;
    return { id, wbsId: state.wbs.length ? state.wbs[0].wbs_id : '', headId: state.budgetHeads.length ? state.budgetHeads[0].budget_head_id : '', description: '', quantity: '1', amountRaw: '', row: null, amountErr: null, qtyErr: null, inrCell: null };
  }

  function parseLine(line) {
    // Whole units only: a foreign order line is emitted as rate x quantity and
    // the server refuses a fractional quantity without a per-unit price
    // (PO_LINE_QUANTITY_NOT_WHOLE); the same discipline on an INR line costs
    // nothing and keeps the two shapes alike.
    const qty = String(line.quantity || '').trim();
    const quantity = /^\d+$/.test(qty) ? Number(qty) : NaN;
    let amountMinor = null;
    let amountError = null;
    try {
      amountMinor = parseAmountToMinor(line.amountRaw, state.exponent, { unit: state.currency === BASE_CURRENCY ? 'rupee' : state.currency });
    } catch (err) {
      amountError = err.message;
    }
    return {
      wbsId: line.wbsId, headId: line.headId, description: String(line.description || '').trim(),
      quantity, quantityError: Number.isInteger(quantity) && quantity > 0 ? null : 'Quantity must be a whole number of units, at least 1.',
      amountMinor, amountError,
    };
  }

  function renderTotals() {
    const parsed = state.lines.map(parseLine);
    const valid = parsed.filter((p) => p.amountMinor !== null && p.amountError === null);
    const totalMinor = valid.reduce((a, p) => a + BigInt(p.amountMinor), 0n);
    const exp = state.exponent;
    if (!isForeign()) {
      state.lines.forEach((line, i) => { line.inrCell.textContent = parsed[i].amountMinor === null ? '—' : formatPaise(parsed[i].amountMinor); });
      totalSource.textContent = valid.length ? formatPaise(totalMinor) : '—';
      totalInr.textContent = valid.length ? formatPaise(totalMinor) : '—';
      return;
    }
    totalSource.textContent = valid.length ? `${state.currency} ${formatMinor(totalMinor, exp)}` : '—';
    if (!state.rate || !valid.length) {
      state.lines.forEach((line) => { line.inrCell.textContent = '—'; });
      totalInr.textContent = '—';
      return;
    }
    const { headerPaise, linePaise } = translateDocument(valid.map((p) => p.amountMinor), state.rate.rate, exp);
    let k = 0;
    state.lines.forEach((line, i) => {
      const ok = parsed[i].amountMinor !== null && parsed[i].amountError === null;
      line.inrCell.textContent = ok ? formatPaise(linePaise[k]) : '—';
      if (ok) k += 1;
    });
    totalInr.textContent = formatPaise(headerPaise);
  }

  function renderLineRow(line) {
    const wbsSelect = h('select', { id: `poLine${line.id}Wbs`, 'aria-label': `Line ${line.id} WBS element`, required: true },
      state.wbs.map((n) => h('option', { value: n.wbs_id, selected: n.wbs_id === line.wbsId }, `${n.wbs_code} — ${n.description || ''}`)));
    const headSelect = h('select', { id: `poLine${line.id}Head`, 'aria-label': `Line ${line.id} budget head`, required: true },
      state.budgetHeads.map((b) => h('option', { value: b.budget_head_id, selected: b.budget_head_id === line.headId }, b.name)));
    const descInput = h('input', { type: 'text', id: `poLine${line.id}Desc`, 'aria-label': `Line ${line.id} description`, value: line.description, maxlength: '200' });
    const qtyInput = h('input', { type: 'text', inputmode: 'numeric', class: 'num po-qty', id: `poLine${line.id}Qty`, 'aria-label': `Line ${line.id} quantity`, value: line.quantity });
    const qtyErr = h('div', { class: 'field-err xs st-negative', hidden: true });
    const amountInput = h('input', { type: 'text', inputmode: 'decimal', class: 'num po-amount', id: `poLine${line.id}Amount`, 'aria-label': `Line ${line.id} ${amountLabel()}`, value: line.amountRaw, autocomplete: 'off' });
    const amountErr = h('div', { class: 'field-err xs st-negative', hidden: true });
    const inrCell = h('td', { class: 'num mono po-inr' }, '—');
    const removeBtn = h('button', { type: 'button', class: 'btn-sm', onClick: () => removeLine(line) }, [
      'Remove', h('span', { class: 'sr-only' }, ` line ${line.id}`),
    ]);
    wbsSelect.addEventListener('change', () => { line.wbsId = wbsSelect.value; });
    headSelect.addEventListener('change', () => { line.headId = headSelect.value; });
    descInput.addEventListener('input', () => { line.description = descInput.value; });
    qtyInput.addEventListener('input', () => { line.quantity = qtyInput.value; qtyErr.hidden = true; clear(qtyErr); });
    amountInput.addEventListener('input', () => {
      line.amountRaw = amountInput.value;
      amountErr.hidden = true; clear(amountErr);
      renderTotals();
    });
    line.row = h('tr', { dataset: { line: String(line.id) } }, [
      h('td', {}, wbsSelect),
      h('td', {}, headSelect),
      h('td', {}, descInput),
      h('td', { class: 'num' }, [qtyInput, qtyErr]),
      h('td', { class: 'num' }, [amountInput, amountErr]),
      inrCell,
      h('td', {}, removeBtn),
    ]);
    line.inrCell = inrCell;
    line.amountErr = amountErr;
    line.qtyErr = qtyErr;
    line.amountInput = amountInput;
    line.qtyInput = qtyInput;
    return line.row;
  }

  function renderLines() {
    clear(linesBody);
    amountHeader.textContent = amountLabel();
    state.lines.forEach((line) => linesBody.appendChild(renderLineRow(line)));
    renderTotals();
  }

  function addLine(focus = true) {
    const line = newLine();
    state.lines.push(line);
    linesBody.appendChild(renderLineRow(line));
    renderTotals();
    syncSubmit();
    if (focus) line.amountInput.focus();
  }

  function removeLine(line) {
    const idx = state.lines.indexOf(line);
    if (idx === -1) return;
    state.lines.splice(idx, 1);
    if (line.row && line.row.isConnected) line.row.remove();
    if (!state.lines.length) addLine();
    else renderTotals();
    announce(`Line ${line.id} removed.`);
  }

  /* ---------------- actions ---------------- */
  const addBtn = h('button', { type: 'button', class: 'btn-sm', id: 'poAddLine', onClick: () => addLine() }, 'Add line');
  const submitBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm', id: 'poSubmit' }, 'Raise purchase order');
  const form = h('form', { id: 'poForm', novalidate: true, 'aria-labelledby': 'poHeaderTitle' }, [
    h('h3', { id: 'poHeaderTitle', class: 'section-title' }, 'Order header'),
    headerForm,
    ratePanel,
    h('h3', { id: 'poLinesTitle', class: 'section-title' }, 'Lines'),
    h('div', { class: 'table-wrap' }, linesTable),
    h('div', { class: 'po-actions' }, [addBtn, submitBtn]),
  ]);

  function syncSubmit() {
    submitBtn.disabled = state.submitting || (isForeign() && !state.rate) || !state.lines.length;
  }

  /* ---------------- currency / date / lookup ---------------- */
  function renderCurrencyOptions() {
    clear(currencySelect);
    state.currencies.forEach((code) => currencySelect.appendChild(h('option', { value: code, selected: code === state.currency }, code)));
    currencyHint.textContent = state.rateBookState === 'error'
      ? 'The rate book could not be read, so only INR is offered.'
      : (state.currencies.length > 1
        ? `${BASE_CURRENCY} default; the others are the rate book's currencies.`
        : 'The rate book quotes no currency yet, so only INR is offered.');
  }

  async function runLookup() {
    state.rate = null; state.rateError = null;
    renderRatePanel(); renderTotals(); syncSubmit();
    if (!isForeign()) return;
    if (!state.documentDate) {
      state.rateError = { code: null, message: 'A document date is required to resolve the rate.' };
      renderRatePanel(); syncSubmit();
      return;
    }
    const seq = state.lookupSeq + 1;
    state.lookupSeq = seq;
    try {
      const res = await lookupRate({ source: state.currency, target: BASE_CURRENCY, date: state.documentDate });
      if (seq !== state.lookupSeq) return;   // the user moved on; a stale answer is not this document's rate
      state.rate = res;
      if (Number.isInteger(res.source_minor_exponent)) state.exponent = res.source_minor_exponent;
      amountHeader.textContent = amountLabel();
      state.lines.forEach((line) => { line.amountInput.setAttribute('aria-label', `Line ${line.id} ${amountLabel()}`); });
      announce(`Rate found: ${res.rate} from ${res.rate_source}.`);
    } catch (err) {
      if (seq !== state.lookupSeq) return;
      const detail = err instanceof FxApiError ? problemDetail(err) : { code: null, message: 'The rate lookup could not be completed.' };
      state.rateError = { code: detail.code, message: detail.message };
      announce(detail.code ? `Refused: ${detail.code}.` : detail.message);
    }
    renderRatePanel(); renderTotals(); syncSubmit();
  }

  currencySelect.addEventListener('change', () => {
    state.currency = currencySelect.value;
    state.exponent = minorExponentOf(state.currency);
    clearError();
    amountHeader.textContent = amountLabel();
    state.lines.forEach((line) => {
      line.amountInput.setAttribute('aria-label', `Line ${line.id} ${amountLabel()}`);
      line.amountErr.hidden = true; clear(line.amountErr);
    });
    runLookup();
  });
  dateInput.addEventListener('change', () => {
    state.documentDate = dateInput.value;
    if (fieldErrs.date) { fieldErrs.date.hidden = true; clear(fieldErrs.date); }
    runLookup();
  });
  projectSelect.addEventListener('change', async () => {
    await loadWbs(projectSelect.value);
    renderLines();
  });

  /* ---------------- submit ---------------- */
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    clearError(); clearFieldErrs(); clear(resultHost);
    let firstInvalid = null;
    const vendorName = vendorInput.value.trim();
    if (!vendorName) { showFieldErr('vendor', 'A purchase order requires a vendor.'); firstInvalid = firstInvalid || vendorInput; }
    if (isForeign() && !state.documentDate) { showFieldErr('date', 'Document date is required.'); firstInvalid = firstInvalid || dateInput; }
    const parsed = state.lines.map(parseLine);
    state.lines.forEach((line, i) => {
      const p = parsed[i];
      if (p.quantityError) { line.qtyErr.hidden = false; clear(line.qtyErr); line.qtyErr.appendChild(text(p.quantityError)); firstInvalid = firstInvalid || line.qtyInput; }
      if (p.amountError) { line.amountErr.hidden = false; clear(line.amountErr); line.amountErr.appendChild(text(p.amountError)); firstInvalid = firstInvalid || line.amountInput; }
    });
    if (firstInvalid) { firstInvalid.focus(); announce('Correct the highlighted fields.'); return; }
    if (isForeign() && !state.rate) { renderRatePanel(); syncSubmit(); return; }

    const payload = buildPurchaseOrderPayload({
      projectId: projectSelect.value, vendorName, poNumber: numberInput.value.trim(),
      currency: state.currency, documentDate: state.documentDate, rate: state.rate,
    }, parsed);

    state.submitting = true; syncSubmit(); submitBtn.textContent = 'Raising…';
    try {
      const doc = await createPurchaseOrder(payload);
      renderResult(doc);
      announce(`${doc.po_number || 'Purchase order'} raised.`);
      state.lines = [];
      renderLines();
      addLine(false);
      numberInput.value = '';
    } catch (err) {
      showError(err);
    } finally {
      state.submitting = false; submitBtn.textContent = 'Raise purchase order'; syncSubmit();
    }
  });

  function renderResult(doc) {
    clear(resultHost);
    const currency = doc.currency || doc.currency_code || BASE_CURRENCY;
    const foreign = currency !== BASE_CURRENCY;
    const exp = foreign && Number.isInteger(doc.source_minor_exponent) ? doc.source_minor_exponent : minorExponentOf(currency);
    const lines = Array.isArray(doc.lines) ? doc.lines : [];
    const table = lines.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'po-result-lines' }, [
      h('caption', { class: 'sr-only' }, 'The lines of the purchase order as written'),
      h('thead', {}, [h('tr', {}, [
        h('th', { scope: 'col' }, 'Line'), h('th', { scope: 'col' }, 'WBS'),
        h('th', { scope: 'col', class: 'num' }, foreign ? `Source (${currency})` : 'Amount'),
        h('th', { scope: 'col', class: 'num' }, 'INR'),
      ])]),
      h('tbody', {}, lines.map((l) => h('tr', {}, [
        h('td', { class: 'num' }, String(l.line_no ?? '')),
        h('td', { class: 'mono' }, l.wbs_code || l.wbs_id || ''),
        h('td', { class: 'num mono' }, foreign ? formatMinor(l.source_amount_minor, exp) : formatPaise(l.amount_paise)),
        h('td', { class: 'num mono' }, formatPaise(l.amount_paise)),
      ]))),
    ])) : null;
    resultHost.appendChild(msgBox('success', h('div', {}, [
      h('div', {}, [h('strong', {}, `${doc.po_number || 'Purchase order'} raised`), ` for ${doc.vendor_name || vendorInput.value.trim()}: ${formatPaise(doc.amount_paise)} committed`, doc.status ? ` (${doc.status})` : '', '.']),
      foreign ? h('div', { class: 'xs' }, [
        `${currency} ${formatMinor(doc.source_amount_minor, exp)} translated at `,
        h('span', { class: 'mono' }, String(doc.exchange_rate ?? '')),
        doc.rate_source ? ` · Source ${doc.rate_source}` : '',
        doc.fx_rate_id ? ` · Rate book entry ${doc.fx_rate_id}` : '',
        doc.fx_rate_date ? ` · ${formatSettingsDate(doc.fx_rate_date)}` : '',
      ]) : null,
      table,
    ].filter(Boolean))));
  }

  /* ---------------- loads ---------------- */
  async function loadWbs(projectId) {
    try {
      state.wbs = projectId ? await getProcurableWbs(projectId) : [];
    } catch (err) {
      state.wbs = [];
      showError(err);
    }
    state.lines.forEach((line) => { if (!state.wbs.some((n) => n.wbs_id === line.wbsId)) line.wbsId = state.wbs.length ? state.wbs[0].wbs_id : ''; });
  }

  async function loadRateBook() {
    const codes = new Set([BASE_CURRENCY]);
    try {
      let cursor = null;
      for (let page = 0; page < 5; page += 1) {
        const res = await listRates({ target: BASE_CURRENCY, limit: 200, cursor: cursor || undefined });
        ((res && res.items) || []).forEach((row) => {
          if (row && row.from_currency && (row.to_currency || BASE_CURRENCY) === BASE_CURRENCY) codes.add(String(row.from_currency).toUpperCase());
        });
        cursor = res && res.next_cursor;
        if (!cursor) break;
      }
      state.rateBookState = 'ready';
    } catch {
      state.rateBookState = 'error';
    }
    state.currencies = [BASE_CURRENCY, ...[...codes].filter((c) => c !== BASE_CURRENCY).sort()];
    renderCurrencyOptions();
  }

  async function load() {
    root.appendChild(h('div', { class: 'po-screen' }, [errorHost, resultHost, form]));
    const [boot] = await Promise.all([getShellBootstrap(), loadRateBook()]);
    state.permissions = boot.permissions;
    state.projects = boot.projects;
    state.budgetHeads = boot.budgetHeads;
    clear(projectSelect);
    state.projects.forEach((p) => projectSelect.appendChild(h('option', { value: p.project_id }, `${p.capex_code || p.project_id} — ${p.name || ''}`)));
    if (!can('po.amend')) {
      clear(errorHost);
      errorHost.appendChild(msgBox('info', h('div', {}, 'Your role cannot raise a purchase order (po.amend), so the form is read-only.')));
      form.querySelectorAll('input, select, button').forEach((el) => { el.disabled = true; });
      return;
    }
    if (state.projects.length) await loadWbs(state.projects[0].project_id);
    if (!state.wbs.length) {
      errorHost.appendChild(msgBox('warning', h('div', {}, 'No WBS element on this project currently allows procurement.')));
    }
    addLine(false);
    renderRatePanel();
    syncSubmit();
  }

  load().catch((err) => { showError(err); });
}
