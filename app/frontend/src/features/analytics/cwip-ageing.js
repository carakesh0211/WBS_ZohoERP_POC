/* app/frontend/src/features/analytics/cwip-ageing.js
   SCR-24 — CWIP Ageing.

   Named verbatim in research/30_contracts/C8_screens.json as "CWIP Ageing",
   so this screen carries its real number.

   WHAT AN AGEING OF CWIP IS ACTUALLY FOR
   --------------------------------------
   Capital work in progress that has sat in CWIP for a long time is the signal
   that an asset is complete and has not been capitalised — which is a real
   financial control, not a curiosity: an uncapitalised asset is not being
   depreciated, and the longer the balance sits the larger the correction when
   somebody notices.

   That makes the buckets the whole value of this screen, and it makes showing
   a fabricated distribution worse here than almost anywhere else: a bucketing
   of one page of bills would put value in the wrong band, and the wrong band
   is precisely the thing the reader is looking at.

   So the measured total is shown — `/api/dashboard`'s `totals.actual`, the
   server's own figure over the caller's resolved scope, which already excludes
   bills that are not accounting-effective — and the distribution is named as
   missing rather than approximated. See `ageing-screen-base.js`.

   The complementary control, "which projects are awaiting capitalisation", is
   a lifecycle question rather than an ageing one and is answered on SCR-20 and
   SCR-21, which belong to another agent in this wave.
*/

import { createAgeingScreen } from './ageing-screen-base.js';
import { getCwipTotal, REPORT_IDS } from './analytics-api.js';

export const mountCwipAgeing = createAgeingScreen({
  screen: 'SCR-24 CWIP Ageing',
  kind: 'cwip',
  idPrefix: 'cwipAge',
  title: 'CWIP Ageing',
  intro: 'Actual CWIP is the value of accounting-effective vendor bills, as the server computes '
    + 'it — bills in a non-effective state are already excluded and are not re-added here. A long '
    + 'ageing on CWIP usually means an asset is complete and has not been capitalised, which is '
    + 'why a wrong distribution would be worse on this screen than a missing one.',
  reportId: REPORT_IDS.cwipAgeing,
  fetchTotal: getCwipTotal,
  rowMetric: 'amount_paise',
  bucketWhat: 'CWIP ageing',
  totals: [
    {
      key: 'actual',
      label: 'Actual CWIP',
      sub: 'The value of accounting-effective vendor bills, summed by the server over your '
        + 'resolved scope.',
      accent: 'info',
      target: 'analytics-cwip-ledger',
    },
    {
      key: 'received_not_billed',
      label: 'Received, not billed',
      sub: 'Value received and not yet invoiced. It is not CWIP yet — it converts when the '
        + 'vendor bills — and is shown so the pending addition is visible.',
      accent: 'watch',
      target: 'analytics-cwip-ledger',
    },
    {
      key: 'commitment',
      label: 'Open commitment',
      sub: 'Ordered less billed. Not CWIP, and never added to it: it is what may still be spent, '
        + 'not what has been.',
      accent: 'info',
      target: 'analytics-commitment-ageing',
    },
  ],
});
