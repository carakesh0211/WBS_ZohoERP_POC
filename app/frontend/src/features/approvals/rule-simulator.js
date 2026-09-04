/* app/frontend/src/features/approvals/rule-simulator.js
   Approval Rule Simulator — "if I raised THIS object, where would it go?"

   The simulator answers a question a configurator otherwise answers by
   raising a real document and watching what happens, which leaves a real
   instance behind on a real object. POST /definitions/{id}/simulate evaluates
   a candidate object against a definition WITHOUT creating an instance.

   THE SIMULATOR MUST SHOW A FAIL-CLOSED OUTCOME AS A FAILURE.

   Contract 2: there is no route to auto-approval. No matching rule gives
   EXCEPTION_PENDING with APPROVAL_ROUTE_UNRESOLVED; an empty approver set
   after the contributor filter gives EXCEPTION_PENDING with
   NO_INDEPENDENT_APPROVER. Both are configuration defects. A simulator that
   rendered either as "no approval required" — or worse, as a green
   "approved" — would teach a configurator that an unroutable object is a fine
   outcome, and the first they would learn otherwise is when a real document
   jammed. So both render as a warning that says the object would NOT be
   approved and names what is missing.
*/

import { h, text, clear } from '../../core/dom.js';
import { approvalStatusBadge } from '../../components/approvals/approval-status-badge.js';
import { createStateHost, stateForError, msgBox } from '../../components/approvals/state-host.js';
import {
  createAnnouncer, field, textInput, select, card, keyValues, replace, queryParam,
} from '../../components/approvals/screen-kit.js';
import { simulate, listDefinitions } from './approvals-api.js';

const SAMPLE = {
  object_type: 'purchase_request',
  entity_id: 'ENT-01',
  project_id: 'PRJ-01',
  amount_paise: 250000000,
  budget_head_id: 'CIVIL',
  maker_user_id: 'U-REQ',
};

/** Outcomes that mean "this object would NOT be approved as configured". */
const UNROUTABLE = new Set(['EXCEPTION_PENDING']);

export function mountRuleSimulator(root) {
  if (!root) return;
  const announce = createAnnouncer('approvalsLiveRegion');

  const state = { definitionId: queryParam('definition'), busy: false };

  const pickSel = select([['', 'Choose a workflow…']], {});
  const pickField = field('simulatorDefinition', 'Workflow', pickSel);

  const objectInput = h('textarea', {
    rows: '10', spellcheck: 'false', class: 'mono',
  });
  objectInput.value = JSON.stringify(SAMPLE, null, 2);
  const objectField = field('simulatorObject', 'Candidate object (JSON)', objectInput);

  const runBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Simulate routing');

  const form = h('form', {
    id: 'simulatorForm',
    'aria-label': 'Simulate how an object would route',
    onSubmit: (ev) => { ev.preventDefault(); run(); },
  }, [
    h('div', { class: 'toolbar approval-toolbar' }, [pickField.el]),
    objectField.el,
    h('div', { class: 'btn-row' }, [runBtn]),
  ]);

  const inputErrorHost = h('div', { id: 'simulatorInputError' });
  const resultHost = h('div', { id: 'simulatorResultHost' });

  const statusHost = createStateHost({
    id: 'simulatorStatusHost',
    glyph: '⊙',
    emptyMessage: 'No simulation has been run yet. Choose a workflow, edit the candidate object and simulate the routing.',
    loadingMessage: 'Simulating the routing…',
    onRetry: () => run(),
  });

  root.appendChild(card('simulatorTitle', 'Approval Rule Simulator', [
    h('p', { class: 'muted small' },
      'A simulation evaluates the routing rules against a candidate object. It '
      + 'creates no approval instance and changes nothing. An object that cannot '
      + 'be routed is reported as an exception, because that is exactly what the '
      + 'engine would do with it — it is never reported as needing no approval.'),
    form, inputErrorHost, statusHost.el, resultHost,
  ]));

  async function fillPicker() {
    try {
      const page = await listDefinitions({ status: 'ACTIVE', limit: 100 });
      const items = Array.isArray(page && page.items) ? page.items : [];
      for (const d of items) {
        if (!d.definition_id) continue;
        pickSel.appendChild(h('option', { value: d.definition_id },
          `${d.code ?? d.definition_id} v${d.version ?? ''} — ${d.object_type ?? ''}`));
      }
      if (!state.definitionId && items.length && items[0].definition_id) {
        state.definitionId = items[0].definition_id;
      }
      if (state.definitionId) pickSel.value = state.definitionId;
    } catch {
      // A picker that could not be filled is not a screen failure. The
      // simulation below reports its own errors truthfully.
    }
  }

  function renderResult(result) {
    const outcome = String((result && result.status) || '').toUpperCase();
    const stages = Array.isArray(result && result.stages) ? result.stages : [];
    clear(resultHost);

    if (UNROUTABLE.has(outcome) || (result && result.code)) {
      const code = (result && result.code) || 'APPROVAL_ROUTE_UNRESOLVED';
      resultHost.appendChild(msgBox('warning', h('div', {}, [
        h('strong', {}, 'This object would NOT be approved. '),
        text(code === 'NO_INDEPENDENT_APPROVER'
          ? 'Every candidate approver for a stage is a contributor on this object, so no independent approver remains. '
          : 'No routing rule matched this object. '),
        text('The engine fails closed: it would hold the object as an exception for an administrator. It would never auto-approve it.'),
      ]), { messageId: code, alert: true }));
    }

    resultHost.appendChild(keyValues([
      ['Outcome', approvalStatusBadge('instance', outcome || 'EXCEPTION_PENDING')],
      ['Matched rule', result && result.matched_rule_priority !== undefined && result.matched_rule_priority !== null
        ? h('span', { class: 'mono' }, `priority ${result.matched_rule_priority}`)
        : text('none')],
      ['Workflow', text(result && result.definition_code
        ? `${result.definition_code} v${result.definition_version ?? ''}`
        : '—')],
      ['Stages that would run', text(String(stages.filter((s) => s.applies !== false).length))],
    ]));

    if (stages.length) {
      resultHost.appendChild(h('div', { class: 'table-wrap', tabindex: '0' }, h('table', {}, [
        h('caption', { class: 'sr-only' },
          'Stages this object would pass through, including stages that would be skipped and why'),
        h('thead', {}, h('tr', {}, [
          h('th', { scope: 'col', class: 'num' }, 'Stage'),
          h('th', { scope: 'col' }, 'Name'),
          h('th', { scope: 'col' }, 'Applies'),
          h('th', { scope: 'col' }, 'Quorum'),
          h('th', { scope: 'col' }, 'Would be assigned to'),
          h('th', { scope: 'col' }, 'Reason not applied'),
        ])),
        h('tbody', {}, stages.map((s) => {
          const applies = s.applies !== false;
          return h('tr', {}, [
            h('td', { class: 'num' }, text(String(s.stage_no ?? '—'))),
            h('td', {}, text(s.name ?? '—')),
            h('td', {}, h('span', { class: `status st-${applies ? 'positive' : 'neutral'}` }, [
              h('span', { class: 'sym', 'aria-hidden': 'true' }, applies ? '✔' : '⊘'),
              text(applies ? 'Yes' : 'Skipped'),
            ])),
            h('td', {}, text(`${s.quorum_type ?? '—'}${s.quorum_n ? ` (${s.quorum_n})` : ''}`)),
            h('td', {}, (Array.isArray(s.would_assign_to) && s.would_assign_to.length)
              ? h('ul', { class: 'approval-assignees' },
                s.would_assign_to.map((u) => h('li', {}, h('span', { class: 'mono' }, String(u)))))
              // An empty approver set is stated, never left blank. A blank cell
              // reads as "nothing to say"; this is the defect itself.
              : h('span', { class: 'status st-warning' }, [
                h('span', { class: 'sym', 'aria-hidden': 'true' }, '!'),
                text('No independent approver'),
              ])),
            h('td', {}, text(s.skip_reason || '—')),
          ]);
        })),
      ])));
    }
  }

  async function run() {
    if (state.busy) return;
    clear(inputErrorHost);
    clear(resultHost);

    const definitionId = pickSel.value || state.definitionId;
    if (!definitionId) {
      inputErrorHost.appendChild(msgBox('error',
        h('div', {}, 'Choose a workflow to simulate against.'), { alert: true }));
      pickSel.focus();
      announce('Choose a workflow to simulate against.');
      return;
    }

    let candidate;
    try {
      candidate = JSON.parse(objectInput.value);
    } catch (e) {
      objectInput.setAttribute('aria-invalid', 'true');
      inputErrorHost.appendChild(msgBox('error',
        h('div', {}, `The candidate object is not valid JSON: ${e.message}`), { alert: true }));
      objectInput.focus();
      announce('The candidate object is not valid JSON.');
      return;
    }
    objectInput.removeAttribute('aria-invalid');

    state.busy = true;
    runBtn.disabled = true;
    statusHost.set('loading');
    announce('Simulating the routing.');
    try {
      const result = await simulate(definitionId, candidate);
      statusHost.set('ready');
      renderResult(result);
      announce(`Simulation complete. Outcome ${(result && result.status) || 'unknown'}.`);
    } catch (err) {
      const mapped = stateForError(err, 'The simulation could not be run. Try again.');
      statusHost.set(mapped.state, mapped);
      announce(mapped.message);
    } finally {
      state.busy = false;
      runBtn.disabled = false;
    }
  }

  replace(resultHost, null);
  statusHost.set('empty');
  fillPicker();
}
