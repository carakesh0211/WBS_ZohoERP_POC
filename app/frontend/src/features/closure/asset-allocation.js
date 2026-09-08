/* SCR-22 — Asset Allocation.
   research/30_contracts/C8_screens.json: "Asset Allocation Screen".

   A WRITE-OFF IS A POSITIVE AMOUNT WITH A FLAG AND A REASON. IT IS NOT A
   NEGATIVE ALLOCATION.

   That rule is enforced three times over, and this screen is the layer where
   it is most likely to be got wrong by a well-meaning shortcut:

     * the database refuses `amount_paise <= 0`
       (`ck_asset_allocation_amount_positive`) and refuses a write-off with no
       reason (`ck_asset_allocation_writeoff_reason`);
     * `pg/closure.py` refuses both before the statement is issued, with a
       message that says WHICH rule was hit rather than a constraint name;
     * this screen offers a checkbox and a reason field, never a minus sign,
       and renders a write-off with a badge -- because a sign is not a
       category. A reader scanning the column has to see WHICH rupees became
       an asset and which did not, and a negative number in a money column
       says neither.

   MONEY IS TYPED IN PAISE AND SENT AS AN INTEGER. There is no rupee field on
   this surface. Parsing "12,34,567.89" in a browser and multiplying by 100 is
   where a paisa goes missing, and `parseFloat` on a money string is the
   canonical way to lose one. The field is `inputmode="numeric"`, the value is
   validated as an integer before it is sent, and the server refuses a float
   outright (NON_INTEGER_AMOUNT) rather than rounding it.

   THE TOTALS ON THIS SCREEN COME FROM THE SERVER. `allocated_paise` and
   `unallocated_paise` are returned by `/api/closure/projects/{project_id}/position`
   and by the allocation response. Nothing here sums a column: a total computed
   in the browser is a total nobody can reconcile against the ledger, and the
   approval gate compares the server's number, not this one.
*/

import { h } from '../../core/dom.js';
import { createAllocation, getPosition, listAllocations } from './closure-api.js';
import { createLoader } from '../integration/integration-screen.js';
import { createAnnouncer, queryParam } from '../integration/integration-kit.js';
import {
  actions, card, field, figure, figures, input, money, postingNote, screen,
  table, writeoffBadge,
} from './closure-kit.js';

const ALLOCATION_COLUMNS = [
  { key: 'allocation_id', label: 'Allocation' },
  { key: 'asset_name', label: 'Asset', wrap: true },
  { key: 'asset_category', label: 'Category' },
  { key: 'wbs_code', label: 'WBS' },
  { key: 'amount_paise', label: 'Amount', numeric: true },
  { key: 'kind', label: 'Kind' },
  { key: 'writeoff_reason', label: 'Write-off reason', wrap: true },
  { key: 'created_by', label: 'Recorded by' },
];

function allocationCell(row, col) {
  if (col.key === 'amount_paise') return money(row.amount_paise);
  if (col.key === 'kind') {
    // The BADGE carries the category. The amount stays positive.
    return row.is_writeoff ? writeoffBadge() : h('span', {}, 'Capitalised');
  }
  const value = row[col.key];
  return (value === null || value === undefined || value === '') ? '—' : String(value);
}

export function mountAssetAllocation(root) {
  if (!root) return;
  const announce = createAnnouncer('closureLiveRegion');

  const projectInput = input('assetAllocationProject', {
    placeholder: 'e.g. PRJ-DM-01',
    value: queryParam('project_id') || '',
  });
  const capInput = input('assetAllocationCapId', {
    placeholder: 'e.g. CAP-…',
    value: queryParam('cap_id') || '',
  });
  const wbsInput = input('assetAllocationWbs', { placeholder: 'e.g. WBS-A-CIVIL' });
  const nameInput = input('assetAllocationName', { placeholder: 'e.g. Substation transformer' });
  const categoryInput = input('assetAllocationCategory', { placeholder: 'e.g. Plant & machinery' });
  const amountInput = input('assetAllocationAmount', {
    inputmode: 'numeric',
    placeholder: 'integer paise, e.g. 500000 for ₹5,000.00',
  });
  const writeoffInput = h('input', { id: 'assetAllocationWriteoff', type: 'checkbox' });
  const reasonInput = input('assetAllocationReason', {
    placeholder: 'required when this is a write-off',
  });

  const positionBody = h('div');
  const allocationsBody = h('div');
  const actionResult = h('div', { id: 'assetAllocationResult' });

  const positionLoader = createLoader({
    id: 'assetAllocationPositionStatus',
    glyph: '★',
    what: 'The allocation position',
    emptyMessage: 'No closure position was returned for this project.',
    loadingMessage: 'Loading the allocation position…',
    onRetry: () => loadPosition(),
    announce,
    idleMessage: 'Enter a project id and a capitalisation request id, then choose Load.',
  });

  const allocationsLoader = createLoader({
    id: 'assetAllocationListStatus',
    glyph: '▤',
    what: 'Asset allocations',
    emptyMessage: 'Nothing has been allocated against this capitalisation request yet. '
      + 'The full CWIP balance is still unallocated.',
    loadingMessage: 'Loading asset allocations…',
    onRetry: () => loadAllocations(),
    announce,
    idleMessage: 'Enter a capitalisation request id and choose Load to see its allocations.',
  });

  root.appendChild(screen([
    card('assetAllocationFilterTitle', 'Capitalisation request', [
      h('div', { class: 'closure-form', role: 'group', 'aria-label': 'Allocation filters' }, [
        field('assetAllocationProject', 'Project id', projectInput),
        field('assetAllocationCapId', 'Capitalisation request id', capInput),
        actions([h('button', {
          type: 'button', class: 'btn-primary btn-sm',
          onClick: () => { loadPosition(); loadAllocations(); },
        }, 'Load')]),
      ]),
    ]),

    card('assetAllocationPositionTitle', 'CWIP balance and what is allocated', [
      positionLoader.el,
      positionBody,
    ]),

    card('assetAllocationFormTitle', 'Record an allocation or a write-off', [
      h('p', { class: 'muted small' },
        'A write-off is a POSITIVE amount carrying the write-off flag and a reason — never a '
        + 'negative allocation. A sign is not a category, and this column has to be readable as '
        + '"what became an asset" and "what did not". The amount is integer paise; there is no '
        + 'rupee field, because parsing one in a browser is where a paisa goes missing.'),
      h('div', { class: 'closure-form', role: 'group', 'aria-label': 'New allocation' }, [
        field('assetAllocationWbs', 'WBS element id', wbsInput),
        field('assetAllocationName', 'Asset name', nameInput, { wide: true }),
        field('assetAllocationCategory', 'Asset category', categoryInput),
        field('assetAllocationAmount', 'Amount (integer paise)', amountInput),
        field('assetAllocationWriteoff', 'This is a write-off', writeoffInput),
        field('assetAllocationReason', 'Write-off reason', reasonInput, { wide: true }),
      ]),
      actions([
        h('button', {
          type: 'button', class: 'btn-primary btn-sm', onClick: () => record(),
        }, 'Record allocation'),
      ]),
      actionResult,
    ]),

    card('assetAllocationListTitle', 'Allocations', [
      allocationsLoader.el,
      allocationsBody,
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
      ]),
      h('p', { class: 'muted small' },
        'Both figures come from the server. Nothing on this screen sums the column below: the '
        + 'approval gate compares the server’s total, and a second total computed here could '
        + 'differ from the one that actually decides.'),
    ]));
    return true;
  }

  async function loadPosition() {
    const id = projectId();
    if (!id) {
      positionLoader.host.set('empty', {
        message: 'Enter a project id and a capitalisation request id, then choose Load.',
      });
      positionBody.hidden = true;
      return;
    }
    await positionLoader.run(() => getPosition(id, capId() || undefined), {
      render: (data) => renderPosition(data),
      onState: (state) => { positionBody.hidden = state !== 'ready'; },
    });
  }

  async function loadAllocations() {
    const id = capId();
    if (!id) {
      allocationsLoader.host.set('empty', {
        message: 'Enter a capitalisation request id and choose Load to see its allocations.',
      });
      allocationsBody.hidden = true;
      return;
    }
    await allocationsLoader.run(() => listAllocations(id), {
      render: (data) => {
        clear(allocationsBody);
        const rows = (data && Array.isArray(data.items)) ? data.items : [];
        if (!rows.length) return false;
        allocationsBody.appendChild(table({
          caption: 'Asset allocations against this capitalisation request, with write-offs '
            + 'marked as such rather than signed',
          columns: ALLOCATION_COLUMNS,
          rows,
          cell: allocationCell,
        }));
        return true;
      },
      onState: (state) => { allocationsBody.hidden = state !== 'ready'; },
    });
  }

  function report(kind, strongText, detail) {
    clear(actionResult);
    actionResult.appendChild(h('div', {
      class: `msg msg-${kind}`, role: kind === 'error' ? 'alert' : 'note',
    }, [
      h('span', { class: 'ico', 'aria-hidden': 'true' }, kind === 'error' ? '✖' : '✔'),
      h('div', { class: 'body' }, [
        h('strong', {}, strongText),
        detail ? h('div', {}, detail) : null,
      ].filter(Boolean)),
    ]));
    announce(strongText);
  }

  /** The amount, as an integer, or null.
   *
   * `Number.isInteger` on a value parsed with `Number()`, not `parseInt`:
   * `parseInt('12.9')` is 12, silently, which is exactly the rounding this
   * surface refuses. A non-integer is rejected here AND by the server.
   */
  function amountPaise() {
    const raw = String(amountInput.value || '').trim();
    if (!raw || !/^\d+$/.test(raw)) return null;
    const n = Number(raw);
    return Number.isSafeInteger(n) && n > 0 ? n : null;
  }

  async function record() {
    const id = capId();
    if (!id) { report('error', 'A capitalisation request id is required.'); return; }
    const amount = amountPaise();
    if (amount === null) {
      report('error', 'The amount must be a positive whole number of paise.',
             'A write-off is a POSITIVE amount with the write-off flag set, not a negative '
             + 'allocation. There is no rupee field here on purpose.');
      return;
    }
    try {
      const result = await createAllocation(id, {
        wbs_id: String(wbsInput.value || '').trim(),
        asset_name: String(nameInput.value || '').trim(),
        asset_category: String(categoryInput.value || '').trim() || null,
        amount_paise: amount,
        is_writeoff: !!writeoffInput.checked,
        writeoff_reason: String(reasonInput.value || '').trim() || null,
      });
      const data = result.data || {};
      report('success',
             `${writeoffInput.checked ? 'Write-off' : 'Allocation'} of ${money(amount)} recorded.`,
             `Allocated to date ${money(data.allocated_paise)}; `
             + `${money(data.unallocated_paise)} still unallocated.`);
      amountInput.value = '';
      loadPosition();
      loadAllocations();
    } catch (err) {
      // The server's own words. A refusal this screen paraphrased is a refusal
      // the operator cannot search for.
      report('error', 'The allocation was refused.', err && err.message ? err.message : '');
    }
  }

  if (projectId()) loadPosition();
  if (capId()) loadAllocations();
}
