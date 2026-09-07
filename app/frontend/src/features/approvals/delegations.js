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

/* ------------------------------------------------------------------ dates */

/*  WHY THIS SCREEN DOES NOT USE <input type="date">
    ------------------------------------------------
    The native control's placeholder follows the HOST OPERATING SYSTEM's
    locale and nothing else. It was measured directly: Playwright's
    `use.locale` sets `navigator.language` and `Intl` correctly while the date
    widget renders pixel-identically under en-IN and en-US, and `--lang` does
    not move it either. So CI drew `mm/dd/yyyy` and an `en-IN` workstation drew
    `dd-mm-yyyy`, and the same build produced two different screens — a 1463 px
    difference on one machine against 969 px on another.

    That is not only a test problem. `dd-mm-yyyy` and `mm/dd/yyyy` disagree
    about what `03-04-2026` MEANS, and a delegation window is a grant of
    authority to act on someone else's behalf: a field whose reading depends on
    the reader's laptop is the wrong field for it. Pinning the CI runner's
    locale would have hidden the difference rather than removed it.

    So the field is the application's own: one text input, one format, stated
    on screen, parsed strictly, and echoed back through the SAME formatter the
    delegation table's `From` column already uses — so what the user is told
    the date means is what the list will show once the delegation exists.
*/

/**
 * The month spellings this field accepts.
 *
 * They are READ BACK OUT OF the application's formatter rather than declared
 * again here. A second month table is a second source of truth, and the day it
 * disagrees with the first is the day the field accepts a spelling the list
 * cannot render. This way the parser can only ever accept what
 * formatAuditTimestamp emits, by construction.
 */
const MONTH_TOKENS = Array.from({ length: 12 }, (_, i) => (
  // '01-Jan-2001 00:00:00 UTC' -> 'Jan'
  formatAuditTimestamp(`2001-${String(i + 1).padStart(2, '0')}-01`).slice(3, 6)
));

const DATE_PATTERN = /^(\d{2})-([A-Za-z]{3})-(\d{4})$/;

/**
 * The format, in the words the screen uses for it. One string, one source: the
 * label of every date field, the sentence under the form, and the message a
 * refusal gives all read from this, so they cannot drift apart.
 */
const DATE_FORMAT = 'DD-MMM-YYYY';

/** The same, with a worked example. The example month comes from the same
 *  table the parser accepts, so it can never be an example the field rejects. */
const DATE_FORMAT_HINT = `${DATE_FORMAT}, for example 01-${MONTH_TOKENS[3]}-2026`;

/**
 * 'DD-MMM-YYYY' -> ISO 'YYYY-MM-DD', or null if it is not exactly that date.
 *
 * Strict on purpose. `1-Apr-26`, `01/04/2026` and `31-Feb-2026` are all
 * refused rather than guessed at, because every guess this function could make
 * is the guess the native widget was making differently on two machines.
 */
function toIsoDate(typed) {
  const m = DATE_PATTERN.exec(String(typed ?? '').trim());
  if (!m) return null;
  const day = Number(m[1]);
  const year = Number(m[3]);
  const monthIndex = MONTH_TOKENS.findIndex(
    (token) => token.toLowerCase() === m[2].toLowerCase(),
  );
  if (monthIndex < 0) return null;
  // 31-Feb-2026 matches the pattern and is not a date. Date.UTC ROLLS OVER
  // rather than refusing — it would hand back 03-Mar — so the components are
  // read back and compared. A date that does not survive the round trip was
  // never the date that was typed. This is done arithmetically rather than by
  // letting the engine parse '2026-02-31', because engines disagree about
  // whether that is Invalid Date or the 3rd of March.
  const d = new Date(Date.UTC(year, monthIndex, day));
  if (d.getUTCFullYear() !== year
    || d.getUTCMonth() !== monthIndex
    || d.getUTCDate() !== day) return null;
  return `${m[3]}-${String(monthIndex + 1).padStart(2, '0')}-${m[1]}`;
}

/**
 * A date field the application controls end to end.
 *
 *   deterministic  — a plain text input. No OS chrome, no host locale, no
 *                    shadow tree. It renders the same on Windows and Linux
 *                    because there is nothing in it the browser chooses.
 *   guided         — the format is IN THE LABEL, so it is permanently visible
 *                    AND part of the accessible name, which a screen reader
 *                    announces every time the field takes focus. A hint that
 *                    is only an aria-description is announced once and read by
 *                    nobody who is looking at the screen.
 *   strict         — toIsoDate() above; anything else is refused, not guessed.
 *   unambiguous    — `.iso` is 'YYYY-MM-DD', which is what the API is given.
 *   keyboard       — one Tab stop, one visible focus ring, nothing to open.
 *
 * WHY THE FORMAT IS IN THE LABEL AND THE READING IS NOT UNDER THE FIELD.
 * Both were tried under the field first. `.toolbar` is a wrapping flex row and
 * `.field` a flex column, so a line of text below the input sets the FIELD's
 * width: a hint reading "DD-MMM-YYYY — for example 01-Apr-2026" made each date
 * field ~215px wide and wrapped the toolbar onto three rows at 1024px, moving
 * the whole screen. The format is four words in an 11px label instead, which
 * leaves the field NARROWER than the native control it replaces, and the
 * resolved reading goes on one full-width line under the toolbar where its
 * length cannot reach the layout.
 *
 * @param {function} onChange called whenever the field's reading changes.
 */
function dateField(id, labelText, describedBy, onChange) {
  const input = textInput({
    maxlength: String(DATE_FORMAT.length),
    size: String(DATE_FORMAT.length),
    'aria-describedby': describedBy,
  });
  const wrap = field(id, `${labelText} (${DATE_FORMAT})`, input);
  input.addEventListener('input', () => onChange());

  return {
    el: wrap.el,
    input,
    get value() { return input.value.trim(); },
    get iso() { return toIsoDate(input.value); },
  };
}

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

  const delegateField = field('delegationDelegate', 'Delegate to (user id)', delegateInput);
  const scopeField = field('delegationScope', 'Scope key', scopeInput);

  /*  THE WINDOW LINE.
      One full-width line under the toolbar, always present, that says what the
      form currently means. Until both dates read, it is the format with a
      worked example; once they do, it is the window itself, rendered by
      formatAuditTimestamp — THE SAME CALL the table's `From` column makes. So
      the user is shown the exact instant the delegation will carry, in the
      exact words the list will use for it once it exists, before they commit
      to granting someone else authority to act for them.

      It is always in the document and it is both fields' accessible
      description, so it is announced on focus and never appears or disappears
      under the pointer. */
  const windowLine = h('p', { id: 'delegationWindow', class: 'muted small' });

  const fromField = dateField('delegationFrom', 'From', 'delegationWindow', updateWindow);
  const toField = dateField('delegationTo', 'To', 'delegationWindow', updateWindow);
  const fromInput = fromField.input;
  const toInput = toField.input;

  function updateWindow() {
    const from = fromField.iso;
    const to = toField.iso;
    windowLine.textContent = (from && to)
      ? `This delegation would run ${formatAuditTimestamp(from)} to ${formatAuditTimestamp(to)}.`
      : `Dates are ${DATE_FORMAT_HINT}.`;
  }
  updateWindow();

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
    windowLine,
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

    // Empty and unreadable are DIFFERENT mistakes and get different messages.
    // "Give the date the delegation starts" is no help at all to someone who
    // typed 03/04/2026 and is looking at a field that says it is wrong.
    for (const [f, what] of [[fromField, 'starts'], [toField, 'ends']]) {
      if (!f.value) {
        problems.push([f.input, `Give the date the delegation ${what}, as ${DATE_FORMAT_HINT}.`]);
      } else if (!f.iso) {
        problems.push([f.input,
          `“${f.value}” is not a date this field can read. Use ${DATE_FORMAT_HINT}.`]);
      }
    }
    // Both ISO, so a string comparison is a date comparison.
    if (fromField.iso && toField.iso && toField.iso < fromField.iso) {
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
        // ISO 'YYYY-MM-DD', never the typed text. validate() has already
        // refused anything `.iso` could not read, so these cannot be null.
        from: fromField.iso,
        to: toField.iso,
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
