/* app/frontend/src/features/approvals/approval-detail.js
   Approval Request Detail — one instance, its stages, and the decision.

   WHY THERE IS NO CLIENT-SIDE PERMISSION CHECK ON THE DECISION PANEL

   Contract 4 is explicit: approval.act is the floor, and "whether a specific
   caller may act on a specific instance is NOT a permission question" — it is
   assignment plus maker-checker, checked per decision inside the transaction,
   with contributor_set(object) removed from every stage's assignee set and a
   delegation checked on BOTH identities so it can never launder a
   self-approval.

   A browser cannot evaluate any of that. It does not know the contributor set,
   it does not know the delegation chain, and it cannot hold the row lock the
   check runs under. So this screen does not try. The decision controls are
   rendered, the server refuses what must be refused (SELF_APPROVAL,
   NOT_AN_ASSIGNEE, STAGE_NOT_OPEN, …), and THAT refusal is what this screen
   renders. Hiding the button on a guess would be a client-side authorization
   decision that is both unenforceable and, when it guessed wrong, invisible.

   Idempotency (Contract 8). The key is minted ONCE when a decision is
   composed and held until that decision succeeds or is abandoned, so a retry
   after a network failure replays the same decision instead of taking a
   second one. A key minted per HTTP call would defeat the entire mechanism.

   Staleness (Contract 8). object_version is sent with every decision and comes
   from the loaded instance. A decision whose version no longer matches is
   refused OBJECT_VERSION_STALE and this screen says so and reloads, rather
   than retrying against a document that moved.
*/

import { h, text, clear } from '../../core/dom.js';
import { formatAuditTimestamp, formatINR } from '../../core/format.js';
import { approvalStatusBadge } from '../../components/approvals/approval-status-badge.js';
import { createStateHost, stateForError, msgBox } from '../../components/approvals/state-host.js';
import {
  createAnnouncer, field, textInput, select, card, keyValues, replace, queryParam,
} from '../../components/approvals/screen-kit.js';
import { createReasonDialog } from '../../components/approvals/reason-dialog.js';
import {
  getInstance, decide, recall, cancel, resubmit, listInbox, newIdempotencyKey,
} from './approvals-api.js';

const ACTIONS = [
  ['APPROVE', 'Approve'],
  ['REJECT', 'Reject'],
  ['RETURN', 'Return for correction'],
];

/* Contract 3's frozen error codes, with wording that tells the user what to do
   next rather than repeating the code at them. An unlisted code falls through
   to the server's own message, which is always shown. */
const CODE_GUIDANCE = {
  SELF_APPROVAL: 'You contributed to this object, so you cannot approve it. It must be decided by someone independent.',
  NOT_AN_ASSIGNEE: 'This stage is not assigned to you.',
  STAGE_NOT_OPEN: 'This stage is no longer open. Reload to see the current position.',
  REASON_REQUIRED: 'This stage requires a reason. Choose a reason code and add the detail.',
  OBJECT_VERSION_STALE: 'The object changed since this approval was loaded. Reload before deciding.',
  IDEMPOTENT_REPLAY: 'This decision had already been recorded. The original outcome is shown; it was not applied twice.',
  BUDGET_MOVED: 'Budget availability moved while this decision was being taken, so it was refused rather than approved against stale numbers.',
  APPROVAL_ROUTE_UNRESOLVED: 'No approval route matched this object. It is held as an exception for an administrator; it has not been approved.',
  NO_INDEPENDENT_APPROVER: 'No independent approver remains for this stage. It is held as an exception for an administrator; it has not been approved.',
};

export function mountApprovalDetail(root) {
  if (!root) return;
  const announce = createAnnouncer('approvalsLiveRegion');
  const reasonDialog = createReasonDialog();

  const state = {
    instanceId: queryParam('instance'),
    instance: null,
    /** Minted once per composed decision; cleared when it lands. */
    idempotencyKey: null,
    busy: false,
  };

  /* ---------------- reference picker ---------------- */

  const refInput = textInput({ value: state.instanceId, placeholder: '' });
  const refField = field('approvalDetailRef', 'Approval reference', refInput);
  const openBtn = h('button', {
    type: 'button', class: 'btn-primary btn-sm', onClick: () => { load(refInput.value.trim()); },
  }, 'Open');

  const toolbar = h('form', {
    class: 'toolbar approval-toolbar',
    onSubmit: (ev) => { ev.preventDefault(); load(refInput.value.trim()); },
    'aria-label': 'Open an approval by reference',
  }, [refField.el, h('div', { class: 'field field-action' }, openBtn)]);

  /* ---------------- hosts ---------------- */

  const statusHost = createStateHost({
    id: 'approvalDetailStatusHost',
    glyph: '✔',
    emptyMessage: 'No approval is open. Enter an approval reference above, or choose one from My Approval Inbox.',
    loadingMessage: 'Loading the approval…',
    onRetry: () => load(state.instanceId),
  });

  const summaryHost = h('div', { id: 'approvalDetailSummary' });
  const stagesHost = h('div', { id: 'approvalDetailStages' });
  const decisionHost = h('div', { id: 'approvalDetailDecision' });
  const outcomeHost = h('div', { id: 'approvalDetailOutcome' });

  root.appendChild(card('approvalDetailTitle', 'Approval Request Detail', [
    toolbar,
    statusHost.el,
    outcomeHost,
    summaryHost,
    stagesHost,
    decisionHost,
  ]));

  /* ---------------- rendering ---------------- */

  function renderSummary(inst) {
    replace(summaryHost, keyValues([
      ['Reference', h('span', { class: 'mono' }, String(inst.instance_id ?? '—'))],
      ['Status', approvalStatusBadge('instance', inst.status)],
      ['Object', h('span', { class: 'mono' }, `${inst.object_type ?? '—'} · ${inst.object_id ?? '—'}`)],
      ['Object version', h('span', { class: 'mono' }, String(inst.object_version ?? '—'))],
      ['Content hash', h('span', { class: 'mono xs', title: String(inst.object_content_sha ?? '') },
        String(inst.object_content_sha ?? '—'))],
      ['Workflow', text(`${inst.definition_code ?? inst.definition_id ?? '—'} v${inst.definition_version ?? '—'}`)],
      ['Value', text(formatINR(inst.amount_paise))],
      ['Raised by', text(inst.maker_user_id ?? '—')],
      ['Opened', text(formatAuditTimestamp(inst.opened_at))],
      ['Closed', text(inst.closed_at ? formatAuditTimestamp(inst.closed_at) : '—')],
      ['Correlation', h('span', { class: 'mono xs' }, String(inst.correlation_id ?? '—'))],
    ]));
  }

  function renderStages(inst) {
    const stages = Array.isArray(inst.stages) ? inst.stages : [];
    clear(stagesHost);
    if (stages.length === 0) return;

    const rows = stages.map((s) => {
      const assignees = Array.isArray(s.assignments) ? s.assignments : [];
      return h('tr', {}, [
        h('td', { class: 'num' }, text(String(s.stage_no ?? '—'))),
        h('td', {}, text(s.name ?? '—')),
        h('td', {}, approvalStatusBadge('stage', s.status)),
        h('td', {}, text(s.quorum_type
          ? `${s.quorum_type}${s.quorum_n ? ` (${s.quorum_n})` : ''} — ${s.quorum_met ?? 0}/${s.quorum_required ?? 0}`
          : '—')),
        h('td', {}, assignees.length
          ? h('ul', { class: 'approval-assignees' }, assignees.map((a) => h('li', {}, [
            h('span', { class: 'mono' }, String(a.assignee_user_id ?? '—')),
            text(' '),
            approvalStatusBadge('assignment', a.state),
            a.assigned_via && a.assigned_via !== 'ROLE'
              ? h('span', { class: 'approval-cell-note' }, ` via ${a.assigned_via}${a.delegated_from ? ` from ${a.delegated_from}` : ''}`)
              : null,
          ].filter(Boolean))))
          : text('—')),
        h('td', {}, text(s.due_at ? formatAuditTimestamp(s.due_at) : '—')),
        // A SKIPPED stage is RECORDED, NEVER OMITTED (C15). Its reason is
        // shown in the same row, because an auditor's question is not "which
        // stages ran" but "which did not, and why".
        h('td', {}, text(s.skip_reason || '—')),
      ]);
    });

    stagesHost.appendChild(h('div', { class: 'table-wrap', tabindex: '0' }, h('table', {}, [
      h('caption', { class: 'sr-only' },
        'Every stage of this approval, including stages that did not apply and the reason they were skipped'),
      h('thead', {}, h('tr', {}, [
        h('th', { scope: 'col', class: 'num' }, 'Stage'),
        h('th', { scope: 'col' }, 'Name'),
        h('th', { scope: 'col' }, 'Status'),
        h('th', { scope: 'col' }, 'Quorum'),
        h('th', { scope: 'col' }, 'Assignees'),
        h('th', { scope: 'col' }, 'Due'),
        h('th', { scope: 'col' }, 'Skip reason'),
      ])),
      h('tbody', {}, rows),
    ])));
  }

  function renderDecision(inst) {
    clear(decisionHost);
    const reasonCodes = Array.isArray(inst.reason_codes) ? inst.reason_codes : [];

    const actionSel = select(ACTIONS, {});
    const actionField = field('approvalDecisionAction', 'Decision', actionSel);

    const reasonSel = select(
      [['', 'No reason code']].concat(reasonCodes.map((r) => [r.code, r.label || r.code])),
      {},
    );
    const reasonField = field('approvalDecisionReasonCode', 'Reason code', reasonSel);

    const reasonText = h('textarea', { rows: '3' });
    const reasonTextField = field('approvalDecisionReasonText', 'Reason detail', reasonText);

    const submit = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Record decision');

    const form = h('form', {
      id: 'approvalDecisionForm',
      class: 'approval-decision',
      'aria-label': 'Record a decision on this approval',
      onSubmit: async (ev) => {
        ev.preventDefault();
        await submitDecision({
          action: actionSel.value,
          reasonCode: reasonSel.value,
          reasonText: reasonText.value.trim(),
          button: submit,
        });
      },
    }, [
      h('p', { class: 'muted small' },
        'Whether you may decide this approval is checked by the server against '
        + 'the assignment and the maker-checker rule at the moment the decision '
        + 'is applied. This form does not pre-judge that.'),
      h('div', { class: 'toolbar approval-toolbar' }, [
        actionField.el, reasonField.el,
      ]),
      reasonTextField.el,
      h('div', { class: 'btn-row' }, [
        submit,
        h('button', {
          type: 'button', class: 'btn-sm',
          onClick: (ev) => lifecycle(recall, 'Recall', ev.currentTarget),
        }, 'Recall'),
        h('button', {
          type: 'button', class: 'btn-sm',
          onClick: (ev) => lifecycle(cancel, 'Cancel approval', ev.currentTarget),
        }, 'Cancel approval'),
        h('button', {
          type: 'button', class: 'btn-sm',
          onClick: (ev) => lifecycle(resubmit, 'Resubmit', ev.currentTarget),
        }, 'Resubmit'),
      ]),
    ]);

    decisionHost.appendChild(card('approvalDecisionTitle', 'Decision', [form]));
  }

  /* ---------------- actions ---------------- */

  function showOutcome(kind, message, { messageId = null } = {}) {
    replace(outcomeHost, msgBox(kind, h('div', {}, message), { messageId, alert: true }));
    announce(message);
  }

  function describeFailure(err) {
    const guidance = err && err.code ? CODE_GUIDANCE[err.code] : null;
    // The server's own message is always shown. Guidance is ADDED beside it,
    // never substituted for it: the server knows things this table does not.
    return guidance ? `${err.message} ${guidance}` : (err && err.message) || 'The decision could not be recorded.';
  }

  async function submitDecision({ action, reasonCode, reasonText: detail, button }) {
    if (state.busy || !state.instance) return;
    state.busy = true;
    button.disabled = true;
    // Minted once, held across retries of THIS decision (Contract 8).
    state.idempotencyKey = state.idempotencyKey || newIdempotencyKey();
    try {
      const result = await decide(state.instance.instance_id, {
        action,
        reasonCode,
        reasonText: detail,
        idempotencyKey: state.idempotencyKey,
        objectVersion: state.instance.object_version,
      });
      state.idempotencyKey = null;
      const replayed = result && result.code === 'IDEMPOTENT_REPLAY';
      showOutcome(replayed ? 'warning' : 'success',
        replayed
          ? `This decision had already been recorded; the original outcome (${result.status || 'unchanged'}) stands. It was not applied twice.`
          : `Decision recorded. The approval is now ${result && result.status ? result.status : 'updated'}.`,
        { messageId: (result && result.message_id) || null });
      await load(state.instance.instance_id, { keepOutcome: true });
    } catch (err) {
      // The key is deliberately NOT cleared on failure: retrying the same
      // decision must replay the same key, not take a second decision.
      if (err && err.code === 'OBJECT_VERSION_STALE') {
        state.idempotencyKey = null;   // a new version needs a new decision
      }
      showOutcome('error', describeFailure(err), { messageId: err && err.messageId });
    } finally {
      state.busy = false;
      button.disabled = false;
    }
  }

  const LIFECYCLE_COPY = {
    Recall: 'Withdraw this approval before a decision is taken. It returns to the requestor as a draft.',
    'Cancel approval': 'Close this approval without a decision. It cannot be reopened; a new one would have to be raised.',
    Resubmit: 'Route this object again, under the workflow version in force now.',
  };

  function lifecycle(fn, label, opener) {
    if (!state.instance) return;
    reasonDialog.open({
      title: label,
      describe: LIFECYCLE_COPY[label] || '',
      confirmLabel: label,
      danger: label === 'Cancel approval',
      opener,
      onConfirm: (reason) => fn(state.instance.instance_id, reason),
      onSuccess: async (result) => {
        showOutcome('success',
          `${label} recorded. The approval is now ${(result && result.status) || 'updated'}.`);
        await load(state.instance.instance_id, { keepOutcome: true });
      },
    });
  }

  /* ---------------- loading ---------------- */

  function clearBody() {
    clear(summaryHost);
    clear(stagesHost);
    clear(decisionHost);
  }

  async function load(instanceId, { keepOutcome = false } = {}) {
    if (!keepOutcome) clear(outcomeHost);
    state.instanceId = instanceId || '';
    refInput.value = state.instanceId;

    if (!state.instanceId) {
      // No reference yet — try the caller's own inbox so the screen opens on
      // something useful rather than on a form. An empty inbox is a genuine
      // empty state, not a failure.
      clearBody();
      statusHost.set('loading');
      try {
        const page = await listInbox({ state: 'OPEN', limit: 1 });
        const first = Array.isArray(page && page.items) ? page.items[0] : null;
        if (first && first.instance_id) return load(first.instance_id, { keepOutcome });
        statusHost.set('empty');
        announce('No approval is open.');
      } catch (err) {
        const mapped = stateForError(err, 'No approval could be opened. Enter a reference above.');
        statusHost.set(mapped.state, mapped);
        announce(mapped.message);
      }
      return undefined;
    }

    clearBody();
    statusHost.set('loading');
    announce('Loading the approval.');
    try {
      const inst = await getInstance(state.instanceId);
      state.instance = inst;
      statusHost.set('ready');
      renderSummary(inst);
      renderStages(inst);
      renderDecision(inst);
      announce(`Approval ${inst.instance_id} loaded. Status ${inst.status}.`);
    } catch (err) {
      state.instance = null;
      clearBody();
      const mapped = stateForError(err, 'This approval could not be loaded. Try again.');
      statusHost.set(mapped.state, mapped);
      announce(mapped.state === 'permission'
        ? 'No approval was found for that reference.'
        : 'This approval could not be loaded.');
    }
    return undefined;
  }

  load(state.instanceId);
}
