/* app/frontend/src/features/budget/budget-availability.js
   SCR-13 Budget Availability Check.

   Enter a WBS element, a budget head and an amount; GET
   /api/budget/availability?wbs_id=&budget_head_id=&amount_paise= returns the
   verdict. All four verdicts (OK | WATCH | CRITICAL | EXCEEDS_BUDGET) are
   rendered through components/budget/verdict-badge.js, which is
   distinguishable without colour — a distinct glyph and a distinct label
   text per verdict, not just a different hue.

   On EXCEEDS_BUDGET the exact shortfall_paise and owning_wbs_id are shown
   verbatim and unrounded: a user refused funding needs to know exactly how
   much is missing and which WBS level actually holds (and therefore
   withheld) the budget, per the build brief's absolute rule for this screen.

   No static fake data: every figure on screen came from a fetch() call.
   Tests intercept the network with Playwright's page.route().
*/

import { checkAvailability, BudgetApiError } from './budget-api.js';
import { h, text, clear } from '../../core/dom.js';
import { formatINR, formatAuditTimestamp } from '../../core/format.js';
import { parseRupeesToPaise } from '../../components/budget/money-input.js';
import { verdictBadge, verdictMeta } from '../../components/budget/verdict-badge.js';

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

export function mountBudgetAvailability(root) {
  if (!root) return;
  const liveRegion = document.getElementById('budgetLiveRegion');

  const state = {
    checkState: 'empty', // 'loading' | 'empty' | 'error' | 'permission' | 'ready'
    error: null,
    result: null,
  };

  const wbsInput = h('input', { id: 'availWbs', type: 'text', autocomplete: 'off', required: true });
  const headInput = h('input', { id: 'availHead', type: 'text', autocomplete: 'off', required: true });
  const amountInput = h('input', {
    id: 'availAmount', type: 'text', inputmode: 'decimal', autocomplete: 'off', required: true,
    placeholder: 'e.g. 25,00,000.00',
  });
  const amountError = h('div', { id: 'availAmountError', class: 'msg msg-error', hidden: true, role: 'alert' }, [
    h('span', { class: 'ico', 'aria-hidden': 'true' }, '✖'),
    h('div', { class: 'body', id: 'availAmountErrorText' }, ''),
  ]);

  const checkBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Check availability');

  const form = h('form', {
    class: 'toolbar',
    'aria-label': 'Budget availability check',
    onSubmit: (ev) => { ev.preventDefault(); runCheck(); },
  }, [
    h('div', { class: 'field' }, [h('label', { for: 'availWbs' }, 'WBS element'), wbsInput]),
    h('div', { class: 'field' }, [h('label', { for: 'availHead' }, 'Budget head'), headInput]),
    h('div', { class: 'field' }, [
      h('label', { for: 'availAmount' }, 'Amount requested (₹)'),
      amountInput,
    ]),
    h('div', { class: 'field field-action' }, [h('span', { class: 'sr-only' }, 'Actions'), checkBtn]),
  ]);

  const resultHost = h('div', { id: 'availResultHost' });

  root.appendChild(form);
  root.appendChild(amountError);
  root.appendChild(resultHost);

  function showFieldError(message) {
    amountInput.setAttribute('aria-invalid', 'true');
    amountInput.setAttribute('aria-describedby', 'availAmountErrorText');
    amountError.hidden = false;
    clear(amountError.querySelector('.body'));
    amountError.querySelector('.body').appendChild(text(message));
  }
  function clearFieldError() {
    amountInput.removeAttribute('aria-invalid');
    amountError.hidden = true;
  }

  function renderResult() {
    clear(resultHost);
    if (state.checkState === 'loading') {
      resultHost.appendChild(h('div', { class: 'loading' }, 'Checking budget availability…'));
      return;
    }
    if (state.checkState === 'empty') {
      resultHost.appendChild(h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '◎'),
        h('div', {}, 'Enter a WBS element, budget head and amount above, then Check availability. '
          + 'No check has been run yet.'),
      ]));
      return;
    }
    if (state.checkState === 'error') {
      const err = state.error;
      resultHost.appendChild(msgBox('error', h('div', {}, err ? err.message : 'The availability check could not be completed.'), {
        messageId: err && err.messageId,
        alert: true,
        actions: [h('button', { type: 'button', class: 'btn-sm', onClick: () => runCheck() }, 'Retry')],
      }));
      return;
    }
    if (state.checkState === 'permission') {
      resultHost.appendChild(h('div', { class: 'empty' }, [
        h('div', { class: 'big', 'aria-hidden': 'true' }, '◎'),
        h('div', {}, state.error ? state.error.message : 'No budget data was found for this WBS element.'),
      ]));
      return;
    }

    // ready
    const r = state.result;
    const meta = verdictMeta(r.verdict);
    const card = h('div', { class: 'card' }, [
      h('h3', {}, [text('Availability result'), h('span', { class: 'spacer' }), verdictBadge(r.verdict, { large: true })]),
      h('div', { class: 'card-body' }, [
        h('p', { class: 'muted' }, meta.description),
        h('dl', { class: 'kv' }, [
          h('dt', {}, 'WBS element'), h('dd', {}, text(r.wbs_id ?? '—')),
          h('dt', {}, 'Budget head'), h('dd', {}, text(r.budget_head_id ?? '—')),
          h('dt', {}, 'Owning WBS'), h('dd', {}, text(r.owning_wbs_id ?? '—')),
          h('dt', {}, 'Approved budget'), h('dd', {}, text(formatINR(r.budget_paise))),
          h('dt', {}, 'Current exposure'), h('dd', {}, text(formatINR(r.exposure_paise))),
          h('dt', {}, 'Available before this request'), h('dd', {}, text(formatINR(r.available_paise))),
          h('dt', {}, 'Amount requested'), h('dd', {}, text(formatINR(r.requested_paise))),
          h('dt', {}, 'Checked at'), h('dd', {}, text(formatAuditTimestamp(r.checked_at))),
        ]),
        r.verdict === 'EXCEEDS_BUDGET' ? h('div', { class: 'msg msg-error', role: 'alert' }, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '⛔'),
          h('div', { class: 'body' }, [
            h('strong', {}, `Shortfall: ${formatINR(r.shortfall_paise)}. `),
            text(`Budget for this request is held at ${r.owning_wbs_id ?? 'a higher WBS level'}, `
              + 'which does not have enough available budget to cover this amount. '
              + 'Submit a budget revision or request exception approval before proceeding.'),
          ]),
        ]) : null,
        (r.verdict === 'WATCH' || r.verdict === 'CRITICAL') ? h('div', { class: 'msg msg-warning' }, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
          h('div', { class: 'body' },
            'This request is fundable, but utilisation on this WBS element is elevated. Review before committing further spend.'),
        ]) : null,
      ].filter(Boolean)),
    ]);
    resultHost.appendChild(card);
  }

  async function runCheck() {
    clearFieldError();
    const wbsId = wbsInput.value.trim();
    const budgetHeadId = headInput.value.trim();
    if (!wbsId || !budgetHeadId) {
      showFieldError('WBS element and budget head are both required.');
      return;
    }
    let amountPaise;
    try {
      amountPaise = parseRupeesToPaise(amountInput.value);
    } catch (err) {
      showFieldError(err.message);
      return;
    }

    state.checkState = 'loading';
    state.error = null;
    checkBtn.disabled = true;
    renderResult();

    try {
      const result = await checkAvailability({ wbsId, budgetHeadId, amountPaise });
      state.result = result;
      state.checkState = 'ready';
      if (liveRegion) {
        const meta = verdictMeta(result.verdict);
        liveRegion.textContent = `Availability check complete: ${meta.label}.`;
      }
    } catch (err) {
      if (err instanceof BudgetApiError && err.kind === 'notfound') {
        state.checkState = 'permission';
        state.error = { message: err.message, messageId: err.messageId };
      } else {
        state.checkState = 'error';
        state.error = {
          message: err instanceof BudgetApiError ? err.message : 'The availability check could not be completed. Try again.',
          messageId: err instanceof BudgetApiError ? err.messageId : null,
        };
      }
      if (liveRegion) liveRegion.textContent = 'The budget availability check could not be completed.';
    }
    checkBtn.disabled = false;
    renderResult();
  }

  renderResult();
}
