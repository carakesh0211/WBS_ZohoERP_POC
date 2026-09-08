/* SCR-21 — Capitalisation Workbench.
   research/30_contracts/C8_screens.json: "Capitalisation Workbench".

   THE MOST MISREADABLE SCREEN IN THE APPLICATION, AND THE ONE THAT SAYS SO.

   Exclusion X-01: this system records the capitalisation DECISION. There is no
   general-ledger posting and no fixed-asset register write anywhere in this
   build. A screen showing a CWIP balance, an Approve button and a green
   success message is one inference away from telling a finance controller that
   money moved. So:

     * `postingNote()` renders above the balance, in the warning tone, on load;
     * every request row carries its own `posting_status`, read off the server's
       row where migration 018's `ck_capitalisation_request_not_posted` pins it;
     * the approval result restates it, in the same words the server used.

   None of those three is a literal typed into this file. If the server ever
   said something else, this screen would show what it said -- which is the
   point of rendering the field rather than the belief.

   THE BLOCKERS ARE THE SERVER'S ARRAY, NOT A SPLIT SENTENCE.
   `/api/closure/projects/{project_id}/position` returns `blockers` as its own
   list and the refusal body repeats it. Deriving the list here by splitting
   prose on semicolons is how a screen starts disagreeing with the gate about
   what is blocking -- and the gate is the authority, because it recomputes
   inside the approving transaction.

   NO CONTROL IS HIDDEN ON A PERMISSION GUESS. Approve and Reject are offered
   to everyone and the server refuses with 403 FORBIDDEN or 403 SELF_APPROVAL.
   A button hidden by a client-side guess makes a refusal look like a missing
   feature, and the client does not hold the answer anyway.
*/

import { h } from '../../core/dom.js';
import {
  approveRequest, createRequest, getPosition, listRequests, rejectRequest,
  submitRequest,
} from './closure-api.js';
import { createLoader } from '../integration/integration-screen.js';
import { createAnnouncer, queryParam } from '../integration/integration-kit.js';
import {
  actions, blockerList, card, field, figure, figures, input, money,
  postingNote, screen, table,
} from './closure-kit.js';

const REQUEST_COLUMNS = [
  { key: 'cap_number', label: 'Request' },
  { key: 'capex_code', label: 'Project' },
  { key: 'status', label: 'Status' },
  { key: 'cwip_balance_paise', label: 'CWIP balance', numeric: true },
  { key: 'allocated_paise', label: 'Allocated', numeric: true },
  { key: 'posting_status', label: 'Posting' },
  { key: 'requested_by', label: 'Raised by' },
  { key: 'approver', label: 'Decided by' },
  { key: 'approved_at', label: 'Decided at' },
  { key: 'decision_note', label: 'Decision note', wrap: true },
];

function requestCell(row, col) {
  if (col.key === 'cwip_balance_paise') return money(row.cwip_balance_paise);
  if (col.key === 'allocated_paise') return money(row.allocated_paise);
  if (col.key === 'posting_status') {
    // Off the row. Not a constant in this file.
    return h('span', {
      class: 'closure-writeoff',
      title: 'This application records the capitalisation decision. No general ledger or '
        + 'fixed-asset posting exists in this build.',
    }, String(row.posting_status || 'UNKNOWN'));
  }
  const value = row[col.key];
  return (value === null || value === undefined || value === '') ? '—' : String(value);
}

export function mountCapitalisationWorkbench(root) {
  if (!root) return;
  const announce = createAnnouncer('closureLiveRegion');

  const projectInput = input('capitalisationProject', {
    placeholder: 'e.g. PRJ-DM-01',
    value: queryParam('project_id') || '',
  });
  const capInput = input('capitalisationCapId', {
    placeholder: 'e.g. CAP-…',
    value: queryParam('cap_id') || '',
  });
  const noteInput = input('capitalisationNote', {
    placeholder: 'the basis for this decision',
  });

  const positionBody = h('div');
  const requestsBody = h('div');
  const actionResult = h('div', { id: 'capitalisationResult' });

  const positionLoader = createLoader({
    id: 'capitalisationPositionStatus',
    glyph: '★',
    what: 'The capitalisation position',
    emptyMessage: 'No closure position was returned for this project.',
    loadingMessage: 'Loading the capitalisation position…',
    onRetry: () => loadPosition(),
    announce,
    idleMessage: 'Enter a project id and choose Load to see what is blocking capitalisation.',
  });

  const requestsLoader = createLoader({
    id: 'capitalisationRequestsStatus',
    glyph: '▤',
    what: 'Capitalisation requests',
    emptyMessage: 'No capitalisation request matches this filter.',
    loadingMessage: 'Loading capitalisation requests…',
    onRetry: () => loadRequests(),
    announce,
  });

  root.appendChild(screen([
    card('capitalisationFilterTitle', 'Project and request', [
      h('div', { class: 'closure-form', role: 'group', 'aria-label': 'Capitalisation filters' }, [
        field('capitalisationProject', 'Project id', projectInput),
        field('capitalisationCapId', 'Capitalisation request id', capInput),
        actions([h('button', {
          type: 'button', class: 'btn-primary btn-sm',
          onClick: () => { loadPosition(); loadRequests(); },
        }, 'Load')]),
      ]),
    ]),

    card('capitalisationPositionTitle', 'What is blocking capitalisation', [
      positionLoader.el,
      positionBody,
    ]),

    card('capitalisationActionsTitle', 'Raise, submit and decide', [
      h('p', { class: 'muted small' },
        'The approval gate recomputes every blocker inside its own transaction, so a blocker '
        + 'that appeared after this screen last refreshed still refuses. A maker cannot approve '
        + 'their own request; that refusal comes from the server, not from a hidden button.'),
      h('div', { class: 'closure-form', role: 'group', 'aria-label': 'Capitalisation actions' }, [
        field('capitalisationNote', 'Decision note', noteInput, { wide: true }),
      ]),
      actions([
        h('button', { type: 'button', class: 'btn-sm', onClick: () => raise() }, 'Raise request'),
        h('button', { type: 'button', class: 'btn-sm', onClick: () => submit() }, 'Submit for approval'),
        h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => approve() }, 'Approve capitalisation'),
        h('button', { type: 'button', class: 'btn-sm', onClick: () => reject() }, 'Reject'),
      ]),
      actionResult,
    ]),

    card('capitalisationRequestsTitle', 'Capitalisation requests', [
      requestsLoader.el,
      requestsBody,
    ]),
  ]));

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  const projectId = () => String(projectInput.value || '').trim();
  const capId = () => String(capInput.value || '').trim();

  function renderPosition(data) {
    clear(positionBody);
    if (!data || !data.project_id) return false;
    positionBody.appendChild(h('div', {}, [
      postingNote(data.posting_status, data.posting_note),
      figures([
        figure('CWIP balance', data.cwip_balance_paise, {
          missingReason: 'The CWIP balance is not available: no derived ledger cell was '
            + 'returned for this project. It is NOT nil.',
        }),
        figure('Allocated', data.allocated_paise, {
          missingReason: 'No capitalisation request was named, so there is no allocation total.',
        }),
        figure('Unallocated', data.unallocated_paise, {
          missingReason: 'No capitalisation request was named, so there is nothing to compare.',
        }),
        figure('Unattributed exceptions', data.open_exceptions
          ? data.open_exceptions.unattributed_paise : null, {
          missingReason: 'The unattributed-exception total could not be read. Capitalisation '
            + 'must not proceed while that is unknown.',
        }),
      ]),
      h('p', { class: 'muted small' },
        'Unattributed receipts are shown at FULL VALUE. They are never spread pro-rata across '
        + 'the projects they might belong to: nobody knows whose they are, and a share would be '
        + 'an invented number on a control screen.'),
      data.blockers && data.blockers.length
        ? h('div', { class: 'msg msg-warning', role: 'note', id: 'capitalisationBlockers' }, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
          h('div', { class: 'body' }, [
            h('strong', {}, `${data.blockers.length} blocker`
              + `${data.blockers.length === 1 ? '' : 's'} stand between this project and `
              + 'capitalisation.'),
            blockerList(data.blockers),
          ]),
        ])
        : h('div', { class: 'msg msg-success', role: 'note', id: 'capitalisationClear' }, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '✔'),
          h('div', { class: 'body' },
            'Nothing is blocking. Approval records the decision locally and posts nothing.'),
        ]),
    ]));
    return true;
  }

  async function loadPosition() {
    const id = projectId();
    if (!id) {
      positionLoader.host.set('empty', {
        message: 'Enter a project id and choose Load to see what is blocking capitalisation.',
      });
      positionBody.hidden = true;
      return;
    }
    await positionLoader.run(() => getPosition(id, capId() || undefined), {
      render: (data) => renderPosition(data),
      onState: (state) => { positionBody.hidden = state !== 'ready'; },
    });
  }

  async function loadRequests() {
    const id = projectId();
    await requestsLoader.run(() => listRequests(id ? { project_id: id } : undefined), {
      render: (data) => {
        clear(requestsBody);
        const rows = (data && Array.isArray(data.items)) ? data.items : [];
        if (!rows.length) return false;
        requestsBody.appendChild(h('div', {}, [
          postingNote(data.posting_status, data.posting_note),
          table({
            caption: 'Capitalisation requests, with the CWIP balance each was raised against '
              + 'and its posting status',
            columns: REQUEST_COLUMNS,
            rows,
            cell: requestCell,
          }),
        ]));
        return true;
      },
      onState: (state) => { requestsBody.hidden = state !== 'ready'; },
    });
  }

  function report(kind, strongText, detail, blockers) {
    clear(actionResult);
    actionResult.appendChild(h('div', {
      class: `msg msg-${kind}`, role: kind === 'error' ? 'alert' : 'note',
      id: 'capitalisationActionResult',
    }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, kind === 'error' ? '✖' : '✔'),
      h('div', { class: 'body' }, [
        h('strong', {}, strongText),
        detail ? h('div', {}, detail) : null,
        blockerList(blockers),
      ].filter(Boolean)),
    ]));
    announce(strongText);
  }

  async function run(label, call, { onSuccess } = {}) {
    try {
      const result = await call();
      if (onSuccess) onSuccess(result.data);
      else report('success', `${label} succeeded.`);
      loadPosition();
      loadRequests();
    } catch (err) {
      const body = err && err.body && typeof err.body === 'object' ? err.body : null;
      const detail = body && typeof body.detail === 'object' ? body.detail : body;
      report('error', `${label} was refused.`,
             err && err.message ? err.message : '',
             detail && Array.isArray(detail.blockers) ? detail.blockers : null);
    }
  }

  function raise() {
    const id = projectId();
    if (!id) { report('error', 'A project id is required to raise a capitalisation request.'); return; }
    run('Raising the request', () => createRequest({ project_id: id }), {
      onSuccess: (data) => {
        if (data && data.cap_id) capInput.value = data.cap_id;
        report('success', `Raised ${data && data.cap_number ? data.cap_number : 'the request'}.`,
               `Posting status: ${data && data.posting_status ? data.posting_status : 'unknown'}.`);
      },
    });
  }

  function submit() {
    const id = capId();
    if (!id) { report('error', 'A capitalisation request id is required to submit.'); return; }
    run('Submitting the request', () => submitRequest(id));
  }

  function approve() {
    const id = capId();
    if (!id) { report('error', 'A capitalisation request id is required to approve.'); return; }
    run('Approving the capitalisation',
        () => approveRequest(id, { note: String(noteInput.value || '').trim() }), {
          onSuccess: (data) => report(
            'success',
            `Capitalisation approved: ${money(data && data.capitalised_paise)}.`,
            // The server's own sentence. Not a constant in this file.
            (data && data.posting_note)
              || 'NOT POSTED - local approval only; no ERP/GL or fixed-asset posting exists '
                 + 'in this build.',
          ),
        });
  }

  function reject() {
    const id = capId();
    if (!id) { report('error', 'A capitalisation request id is required to reject.'); return; }
    run('Rejecting the request',
        () => rejectRequest(id, { note: String(noteInput.value || '').trim() }));
  }

  loadRequests();
  if (projectId()) loadPosition();
}
