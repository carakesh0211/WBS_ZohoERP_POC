/* app/frontend/src/features/analytics/commitment-ageing.js
   SCR-23 — Open Commitment Ageing.

   Named verbatim in research/30_contracts/C8_screens.json as "Open Commitment
   Ageing", so this screen carries its real number.

   OPEN COMMITMENT IS ORDERED LESS BILLED
   --------------------------------------
   Not ordered less received. A receipt does not release a commitment; a bill
   does. Getting that backwards understates open commitment on every partially
   received line, which is the number the whole CAPEX control exists to
   protect — and this screen is where an ageing analysis would make the
   understatement look like a trend.

   Nothing here computes it. The total comes from `/api/reconciliation`'s own
   summary, which derives it from `domain.compute_ledger`, the frozen C5
   registry. `received_not_billed` is shown beside it as a SEPARATE card and
   the two are never added: they overlap by design.

   WHY THE BUCKETS ARE NOT SHOWN
   -----------------------------
   See `ageing-screen-base.js`. In short: an ageing table is a grouped
   aggregate over the caller's whole scope, nothing in this build produces one,
   and bucketing a page of documents in the browser would put page totals under
   portfolio headings. The total that IS measured is shown; the distribution
   that is not is named as missing, by route.
*/

import { createAgeingScreen } from './ageing-screen-base.js';
import { getCommitmentTotal, SCREENS } from './analytics-api.js';

export const mountCommitmentAgeing = createAgeingScreen({
  screen: 'SCR-23 Open Commitment Ageing',
  kind: 'commitment',
  idPrefix: 'comAge',
  title: 'Open Commitment Ageing',
  intro: 'Open commitment is ordered less BILLED, floored at zero, and zero outright once a '
    + 'purchase order reaches a commitment-releasing state — a receipt does not release a '
    + 'commitment. Received-not-billed is a separate exposure shown beside it; the two overlap '
    + 'by design and are never added together.',
  report: SCREENS.commitmentAgeing,
  fetchTotal: getCommitmentTotal,
  rowMetric: 'open_commitment_paise',
  bucketWhat: 'Open commitment ageing',
  totals: [
    {
      key: 'open_commitment',
      label: 'Open commitment',
      sub: 'Ordered less billed, floored at zero, released orders excluded. The server\'s own '
        + 'sum over your resolved scope.',
      accent: 'info',
      target: 'analytics-controller',
    },
    {
      key: 'billed',
      label: 'Billed against these orders',
      sub: 'What has already been invoiced against the same lines.',
      accent: 'info',
      target: 'analytics-cwip-ledger',
    },
    {
      key: 'received_not_billed',
      label: 'Received, not billed',
      sub: 'Received and not yet invoiced. A SEPARATE exposure — it overlaps open commitment and '
        + 'the two are never added together.',
      accent: 'watch',
      target: 'analytics-cwip-ledger',
    },
  ],
});
