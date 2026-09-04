/* app/frontend/src/features/approvals/delegations.js
   Delegation Management — who may act for whom, over what scope, for how long.

   Contract 5: a delegation checks BOTH identities. A decision is refused if
   the actor OR the person acted for is a contributor on the object, so a
   delegation can never launder a self-approval. That is enforced server-side
   inside the decision transaction; this screen's job is to make the delegation
   itself visible and revocable, and to say plainly that a delegation is not a
   way around maker-checker — because that is exactly what a delegation looks
   like it might be.

   A delegation is never deleted. `revoked_at` and `revoke_reason` are recorded
   and the row stays, because "who could act for whom last March" is an audit
   question, and a deleted row cannot answer it. There is therefore no delete
   control here, only Revoke.
*/

import { h, text, clear } from '../../core/dom.js';
import { formatAuditTimestamp } from '../../core/format.js';
import { createDataTable } from '../../components/capex-datatable.js';
import { createStateHost, stateForError, msgBox } from '../../components/approvals/state-host.js';
import {
  createAnnouncer, createPagination, field, textInput, card,
} from '../../components/approvals/screen-kit.js';
import { createReasonDialog } from '../../components/approvals/reason-dialog.js';
import { listDelegations, createDelegation, revokeDelegation } from './approvals-api.js';

const PAGE_SIZE = 25;

function delegationStatus(row) {
  if (row.revoked_at) {
    return h('span', { class: 'status st-neutral', title: row.revoke_reason || 'Revoked' }, [
      h('span', { class: 'sym', 'aria-hidden': 'true' }, '⊘'),
      text('Revoked'),
    ]);
  }
  const active = row.active !== false;
  return h('span', { class: `status st-${active ? 'positive' : 'neutral'}` }, [
    h('span', { class: 'sym', 'aria-hidden': 'true' }, active ? '✔' : '·'),
    text(active ? 'Active' : 'Not yet in force'),
  ]);
}

export function mountDelegations(root) {
  if (!root) return;
  const announce = createAnnouncer('approvalsLiveRegion');
  const reasonDialog = createReasonDialog();

  const state = { rows: [], cursor: null, hasMore: false, loading: false, busy: false };

  /* ---------------- create ---------------- */

  const delegateInput = textInput({});
  const scopeInput = textInput({});
  const fromInput = h('input', { type: 'date' });
  const toInput = h('input', { type: 'date' });

  const delegateField = field('delegationDelegate', 'Delegate to (user id)', delegateInput);
  const scopeField = field('delegationScope', 'Scope key', scopeInput);
  const fromField = field('delegationFrom', 'From', fromInput);
  const toField = field('delegationTo', 'To', toInput);

  const createBtn = h('button', { type: 'submit', class: 'btn-primary btn-sm' }, 'Create delegation');
  const formErrorHost = h('div', { id: 'delegationFormError' });

  const form = h('form', {
    id: 'delegationForm',
    'aria-label': 'Create a delegation',
    onSubmit: (ev) => { ev.preventDefault(); create(); },
  }, [
    h('div', { class: 'toolbar approval-toolbar' }, [
      delegateField.el, scopeField.el, fromField.el, toField.el,
      h('div', { class: 'field field-action' }, createBtn),
    ]),
    formErrorHost,
  ]);

  /* ---------------- list ---------------- */

  const table = createDataTable({
    caption: 'Delegations, their scope, their active window and whether they have been revoked',
    emptyMessage: 'No delegations are recorded.',
    columns: [
      // The id is on the row because this list is read as a register: a
      // reviewer cites a delegation by id, and the revoke call is addressed by
      // it. A row that cannot be named cannot be discussed.
      { key: 'delegation_id', label: 'Reference', render: (r) => h('span', { class: 'mono' }, String(r.delegation_id ?? '—')) },
      { key: 'delegator_user_id', label: 'Delegated by', render: (r) => h('span', { class: 'mono' }, String(r.delegator_user_id ?? '—')) },
      { key: 'delegate_user_id', label: 'Delegated to', render: (r) => h('span', { class: 'mono' }, String(r.delegate_user_id ?? '—')) },
      { key: 'scope_key', label: 'Scope', render: (r) => h('span', { class: 'mono' }, String(r.scope_key ?? 'all')) },
      { key: 'from', label: 'From', render: (r) => text(r.from ? formatAuditTimestamp(r.from) : '—') },
      { key: 'to', label: 'To', render: (r) => text(r.to ? formatAuditTimestamp(r.to) : 'open') },
      { key: 'status', label: 'Status', render: (r) => delegationStatus(r) },
      {
        key: 'revoke_reason',
        label: 'Revoked because',
        render: (r) => text(r.revoke_reason || '—'),
      },
      {
        key: 'actions',
        label: 'Action',
        render: (r) => (r.revoked_at
          // A revoked delegation stays on the list. It is history, and it is
          // the answer to "who could act for whom, and when".
          ? h('span', { class: 'approval-cell-note' }, `Revoked ${formatAuditTimestamp(r.revoked_at)}`)
          : h('button', {
            type: 'button', class: 'btn-sm btn-danger',
            onClick: (ev) => revoke(r, ev.currentTarget),
          }, 'Revoke')),
      },
    ],
  });

  const statusHost = createStateHost({
    id: 'delegationsStatusHost',
    glyph: '⇄',
    emptyMessage: 'No delegations are recorded. Create one above to let someone act on your behalf for a fixed period.',
    loadingMessage: 'Loading delegations…',
    onRetry: () => load(true),
  });

  const pagination = createPagination({ id: 'delegationsPagination', onMore: () => load(false) });
  const outcomeHost = h('div', { id: 'delegationsOutcome' });

  root.appendChild(card('delegationsTitle', 'Delegation Management', [
    h('p', { class: 'muted small' },
      'A delegation lets someone act on your behalf within a scope, for a fixed '
      + 'period. It is not a way around separation of duties: a decision is '
      + 'refused if either the person acting or the person they act for '
      + 'contributed to the object, so a delegation cannot be used to approve '
      + 'your own work.'),
    form, outcomeHost, statusHost.el, table.el, pagination.el,
  ]));

  /* ---------------- actions ---------------- */

  function showOutcome(kind, message, messageId) {
    clear(outcomeHost);
    outcomeHost.appendChild(msgBox(kind, h('div', {}, message), { messageId: messageId || null, alert: true }));
    announce(message);
  }

  function validate() {
    const problems = [];
    if (!delegateInput.value.trim()) problems.push([delegateInput, 'Name the user the authority is delegated to.']);
    if (!fromInput.value) problems.push([fromInput, 'Give the date the delegation starts.']);
    if (!toInput.value) problems.push([toInput, 'Give the date the delegation ends.']);
    if (fromInput.value && toInput.value && toInput.value < fromInput.value) {
      problems.push([toInput, 'The end date cannot be before the start date.']);
    }
    for (const el of [delegateInput, fromInput, toInput]) el.removeAttribute('aria-invalid');
    clear(formErrorHost);
    if (problems.length === 0) return true;
    for (const [el] of problems) el.setAttribute('aria-invalid', 'true');
    formErrorHost.appendChild(msgBox('error',
      h('ul', { class: 'field-err-list' }, problems.map(([el, m]) => h('li', {},
        h('a', {
          href: '#',
          onClick: (ev) => { ev.preventDefault(); el.focus(); },
        }, m)))),
      { alert: true }));
    problems[0][0].focus();
    return false;
  }

  async function create() {
    if (state.busy || !validate()) return;
    state.busy = true;
    createBtn.disabled = true;
    try {
      await createDelegation({
        delegateUserId: delegateInput.value.trim(),
        scopeKey: scopeInput.value.trim() || null,
        from: fromInput.value,
        to: toInput.value,
      });
      showOutcome('success', `Delegation to ${delegateInput.value.trim()} recorded.`);
      delegateInput.value = '';
      scopeInput.value = '';
      await load(true);
    } catch (err) {
      showOutcome('error',
        err && err.code === 'DELEGATION_WINDOW_INVALID'
          ? `${err.message} Check that the period does not overlap an existing delegation for the same scope.`
          : (err && err.message) || 'The delegation could not be created.',
        err && err.messageId);
    } finally {
      state.busy = false;
      createBtn.disabled = false;
    }
  }

  function revoke(row, opener) {
    reasonDialog.open({
      title: 'Revoke delegation',
      describe: `${row.delegate_user_id} will no longer be able to act for `
        + `${row.delegator_user_id}. The delegation stays on this list as a record `
        + 'of who could act for whom, and when.',
      confirmLabel: 'Revoke',
      danger: true,
      opener,
      onConfirm: (reason) => revokeDelegation(row.delegation_id, reason),
      onSuccess: async () => {
        showOutcome('success', 'The delegation was revoked. It stays on this list as a record.');
        await load(true);
      },
    });
  }

  async function load(reset) {
    if (state.loading) return;
    state.loading = true;
    if (reset) {
      state.rows = []; state.cursor = null; state.hasMore = false;
      statusHost.set('loading');
      table.el.hidden = false;
      table.renderSkeleton();
      announce('Loading delegations.');
    }
    pagination.set({ hasMore: state.hasMore, loading: true, shown: state.rows.length });

    try {
      const page = await listDelegations({
        cursor: reset ? undefined : state.cursor || undefined,
        limit: PAGE_SIZE,
      });
      const items = Array.isArray(page && page.items) ? page.items : [];
      state.rows = reset ? items : state.rows.concat(items);
      state.cursor = (page && page.next_cursor) || null;
      state.hasMore = !!(page && page.has_more);

      if (state.rows.length === 0) {
        // Clear the skeleton rather than just hiding it: a hidden stale
        // skeleton still matches a query and still reads as "loading".
        table.renderRows([]);
        table.el.hidden = true;
        statusHost.set('empty');
        announce('No delegations are recorded.');
      } else {
        statusHost.set('ready');
        table.el.hidden = false;
        table.renderRows(state.rows);
        announce(`${state.rows.length} delegation${state.rows.length === 1 ? '' : 's'} shown.`);
      }
    } catch (err) {
      // Clear the skeleton rather than just hiding it: a hidden stale
      // skeleton still matches a query and still reads as "loading".
      table.renderRows([]);
      table.el.hidden = true;
      const mapped = stateForError(err, 'Delegations could not be loaded. Try again.');
      statusHost.set(mapped.state, mapped);
      announce(mapped.state === 'permission'
        ? 'No delegations were found.'
        : 'Delegations could not be loaded.');
    } finally {
      state.loading = false;
      pagination.set({ hasMore: state.hasMore, loading: false, shown: state.rows.length });
    }
  }

  load(true);
}
