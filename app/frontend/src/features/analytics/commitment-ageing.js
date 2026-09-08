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
  rowMetric: 'commitment',
  bucketWhat: 'Open commitment ageing',
  /* THE KEYS ARE THE PREFERRED SOURCE'S, NOT THE FALLBACK'S.
     These were `open_commitment` and `billed` — the names `/api/reconciliation`
     uses, and names `/api/reports/metrics` has never returned. The screen
     therefore rendered real figures on the DEGRADED path and "not reported" on
     the good one, which is the failure that hides itself: the fallback is what
     a SQLite build shows, so the broken case only appeared once the reporting
     database existed. SCR-24 was already keyed on the canonical vocabulary
     (`domain._COMPONENTS`) and was right on both sources.
     `analytics-api.js::canonicaliseReconciliation` now renames the fallback's
     summary into these names, so both sources answer to one vocabulary and
     neither screen has to know which one replied. */
  totals: [
    {
      key: 'commitment',
      label: 'Open commitment',
      sub: 'Ordered less billed, floored at zero, released orders excluded. The server\'s own '
        + 'sum over your resolved scope.',
      accent: 'info',
      target: 'analytics-controller',
    },
    {
      key: 'actual',
      label: 'Billed against these orders',
      sub: 'What has already been invoiced. The reporting service reports every '
        + 'accounting-effective vendor bill in scope; the reconciliation fallback reports the '
        + 'part of that value attributable to a purchase-order line. The source line above says '
        + 'which one answered.',
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
