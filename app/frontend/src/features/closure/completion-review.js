/* SCR-20 — Project Completion Review.
   research/30_contracts/C8_screens.json: "Project Completion Review".

   TWO PANELS, TWO INDEPENDENT LOADS, SIX STATES EACH.

   The position and the review list are different reads of different tables and
   either can fail on its own. Driving both from one loader would mean a
   failure in either renders the other as broken -- and, worse, a build that
   mounts the review routes but not the position route would show "unavailable"
   over a working review list. Two loaders, each naming its own route.

   WHAT THIS SCREEN IS FOR
   -----------------------
   Technical completion is ASSERTED by the project side and DECIDED by someone
   else. Those are two acts by two people, which is why they are two buttons
   and why the server refuses a decision by the person who asserted
   (SELF_APPROVAL, 403). This screen offers both controls to everyone and lets
   the server refuse: a control hidden on the client is a guess about a
   permission the client does not hold the answer to, and hiding it makes a
   refusal look like a missing feature.

   THE COMPLETION DATE IS TYPED, NOT DERIVED. Nothing in this system knows when
   the last bolt was tightened. Defaulting it from the newest GRN would put a
   fabricated fact on a control record, so the field is empty and the server
   refuses a submission without it (COMPLETION_DATE_REQUIRED).

   AND NOTHING HERE IS POSTED ANYWHERE. The posting note is rendered above the
   CWIP figure, on this screen as on SCR-21, because a reader who sees a
   balance and an Accept button is one inference away from believing a ledger
   moved.
*/

import { h } from '../../core/dom.js';
import { getPosition, listReviews, createReview, submitReview, decideReview } from './closure-api.js';
import { createLoader } from '../integration/integration-screen.js';
import { createAnnouncer, queryParam } from '../integration/integration-kit.js';
import {
  actions, blockerList, card, field, figure, figures, input,
  postingNote, screen, table,
} from './closure-kit.js';

const REVIEW_COLUMNS = [
  { key: 'review_id', label: 'Review' },
  { key: 'capex_code', label: 'Project' },
  { key: 'status', label: 'Status' },
  { key: 'completion_date', label: 'Completion asserted' },
  { key: 'submitted_by', label: 'Asserted by' },
  { key: 'decided_by', label: 'Decided by' },
  { key: 'decided_at', label: 'Decided at' },
  { key: 'summary', label: 'Summary', wrap: true },
  { key: 'decision_note', label: 'Decision note', wrap: true },
];

export function mountCompletionReview(root) {
  if (!root) return;
  const announce = createAnnouncer('closureLiveRegion');

  const projectInput = input('completionReviewProject', {
    placeholder: 'e.g. PRJ-DM-01',
    value: queryParam('project_id') || '',
  });
  const reviewInput = input('completionReviewId', {
    placeholder: 'e.g. PCR-…',
    value: queryParam('review_id') || '',
  });
  const dateInput = input('completionReviewDate', {
    placeholder: 'yyyy-mm-dd',
  });
  const noteInput = input('completionReviewNote', {
    placeholder: 'why this decision was taken',
  });

  const positionBody = h('div');
  const reviewsBody = h('div');
  const actionResult = h('div', { id: 'completionReviewResult' });

  const positionLoader = createLoader({
    id: 'completionReviewPositionStatus',
    glyph: '◷',
    what: 'The project closure position',
    emptyMessage: 'No closure position was returned for this project.',
    loadingMessage: 'Loading the project closure position…',
    onRetry: () => loadPosition(),
    announce,
    idleMessage: 'Enter a project id and choose Load to see its closure position.',
  });

  const reviewsLoader = createLoader({
    id: 'completionReviewListStatus',
    glyph: '⧉',
    what: 'Project completion reviews',
    emptyMessage: 'No completion review has been raised for this filter.',
    loadingMessage: 'Loading completion reviews…',
    onRetry: () => loadReviews(),
    announce,
  });

  root.appendChild(screen([
    card('completionReviewFilterTitle', 'Project', [
      h('div', { class: 'closure-form', role: 'group', 'aria-label': 'Project filter' }, [
        field('completionReviewProject', 'Project id', projectInput),
        actions([h('button', {
          type: 'button', class: 'btn-primary btn-sm', onClick: () => { loadPosition(); loadReviews(); },
        }, 'Load')]),
      ]),
    ]),

    card('completionReviewPositionTitle', 'Closure position', [
      positionLoader.el,
      positionBody,
    ]),

    card('completionReviewActionsTitle', 'Assert and decide completion', [
      h('p', { class: 'muted small' },
        'Technical completion is asserted by the project side and decided by someone else. '
        + 'The server refuses a decision taken by the person who asserted it, and it refuses a '
        + 'submission with no completion date — that date is typed here because nothing in this '
        + 'system knows it.'),
      h('div', { class: 'closure-form', role: 'group', 'aria-label': 'Completion review actions' }, [
        field('completionReviewId', 'Review id', reviewInput),
        field('completionReviewDate', 'Completion date', dateInput),
        field('completionReviewNote', 'Decision note', noteInput, { wide: true }),
      ]),
      actions([
        h('button', { type: 'button', class: 'btn-sm', onClick: () => raise() }, 'Raise review'),
        h('button', { type: 'button', class: 'btn-sm', onClick: () => submit() }, 'Assert completion'),
        h('button', { type: 'button', class: 'btn-primary btn-sm', onClick: () => decide('Accepted') }, 'Accept'),
        h('button', { type: 'button', class: 'btn-sm', onClick: () => decide('Rejected') }, 'Reject'),
      ]),
      actionResult,
    ]),

    card('completionReviewListTitle', 'Completion reviews', [
      reviewsLoader.el,
      reviewsBody,
    ]),
  ]));

  function clear(el) {
    while (el.firstChild) el.removeChild(el.firstChild);
  }

  function projectId() {
    return String(projectInput.value || '').trim();
  }

  function renderPosition(data) {
    clear(positionBody);
    if (!data || !data.project_id) return false;
    positionBody.appendChild(h('div', {}, [
      // X-01, above the money, every time.
      postingNote(data.posting_status, data.posting_note),
      figures([
        figure('CWIP balance', data.cwip_balance_paise, {
          missingReason: 'The CWIP balance is not available: no derived ledger cell was '
            + 'returned for this project.',
        }),
        figure('Open commitment', data.open_commitment_paise, {
          missingReason: 'Open commitment is not available for this project.',
        }),
        figure('Received not billed', data.received_not_billed_paise, {
          missingReason: 'Received-not-billed is not available for this project.',
        }),
        figure('Held by live PR reservations', data.pr_reserved_paise, {
          missingReason: 'Live purchase-request holds are not available for this project.',
        }),
        figure('Internal allocation (open)', data.internal_allocation_paise, {
          missingReason: 'Internal allocation is not available for this project.',
        }),
        figure('Internal consumption (CWIP)', data.internal_consumption_paise, {
          missingReason: 'Internal consumption is not available for this project.',
        }),
      ]),
      h('p', { class: 'muted small' }, [
        'Project status ',
        h('span', { class: 'mono' }, String(data.project_status || '—')),
        '. Accepted completion review: ',
        h('span', { class: 'mono' }, String(data.accepted_review_id || 'none')),
        '.',
      ]),
      data.blockers && data.blockers.length
        ? h('div', { class: 'msg msg-warning', role: 'note', id: 'completionReviewBlockers' }, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '!'),
          h('div', { class: 'body' }, [
            h('strong', {}, 'This project cannot be capitalised yet.'),
            blockerList(data.blockers),
          ]),
        ])
        : h('div', { class: 'msg msg-success', role: 'note', id: 'completionReviewClear' }, [
          h('span', { class: 'ico', 'aria-hidden': 'true' }, '✔'),
          h('div', { class: 'body' },
            'Nothing is blocking capitalisation. The decision would still be recorded locally '
            + 'and posted nowhere.'),
        ]),
    ]));
    return true;
  }

  async function loadPosition() {
    const id = projectId();
    if (!id) {
      positionLoader.host.set('empty', {
        message: 'Enter a project id and choose Load to see its closure position.',
      });
      positionBody.hidden = true;
      return;
    }
    await positionLoader.run(() => getPosition(id), {
      render: (data) => renderPosition(data),
      onState: (state) => { positionBody.hidden = state !== 'ready'; },
    });
  }

  async function loadReviews() {
    const id = projectId();
    await reviewsLoader.run(() => listReviews(id ? { project_id: id } : undefined), {
      render: (data) => {
        clear(reviewsBody);
        const rows = (data && Array.isArray(data.items)) ? data.items : [];
        if (!rows.length) return false;
        reviewsBody.appendChild(table({
          caption: 'Project completion reviews, with who asserted completion and who decided it',
          columns: REVIEW_COLUMNS,
          rows,
          cell: (row, col) => {
            const value = row[col.key];
            return (value === null || value === undefined || value === '') ? '—' : String(value);
          },
        }));
        return true;
      },
      onState: (state) => { reviewsBody.hidden = state !== 'ready'; },
    });
  }

  /** Render whatever the server said, success or refusal, verbatim. */
  function report(kind, strongText, detailText) {
    clear(actionResult);
    actionResult.appendChild(h('div', {
      class: `msg msg-${kind}`, role: kind === 'error' ? 'alert' : 'note',
    }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, kind === 'error' ? '✖' : '✔'),
      h('div', { class: 'body' }, [
        h('strong', {}, strongText),
        detailText ? h('div', {}, detailText) : null,
      ].filter(Boolean)),
    ]));
    announce(strongText);
  }

  async function run(label, call) {
    try {
      const result = await call();
      report('success', `${label} succeeded.`,
             JSON.stringify(result.data, null, 0).slice(0, 400));
      loadPosition();
      loadReviews();
    } catch (err) {
      // The server's own words, never a rewrite. A refusal this screen
      // paraphrased is a refusal the operator cannot search for.
      report('error', `${label} was refused.`, err && err.message ? err.message : '');
    }
  }

  function raise() {
    const id = projectId();
    if (!id) { report('error', 'A project id is required to raise a review.'); return; }
    run('Raising the review', () => createReview({
      project_id: id, summary: String(noteInput.value || '').trim() || null,
    }));
  }

  function submit() {
    const id = String(reviewInput.value || '').trim();
    if (!id) { report('error', 'A review id is required to assert completion.'); return; }
    run('Asserting completion', () => submitReview(id, {
      completion_date: String(dateInput.value || '').trim(),
    }));
  }

  function decide(decision) {
    const id = String(reviewInput.value || '').trim();
    if (!id) { report('error', `A review id is required to ${decision.toLowerCase()} a review.`); return; }
    run(`${decision === 'Accepted' ? 'Accepting' : 'Rejecting'} the review`,
        () => decideReview(id, {
          decision, note: String(noteInput.value || '').trim(),
        }));
  }

  loadReviews();
  if (projectId()) loadPosition();
}
