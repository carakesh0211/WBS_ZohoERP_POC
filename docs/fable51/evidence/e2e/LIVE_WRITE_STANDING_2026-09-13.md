# Standing LIVE_WRITE configuration — verified read-only (2026-09-13 IST)

Product-owner decision of 2026-09-13: outbound purchase-order creation stays
enabled for ongoing UAT against DEMO WBS (60074128927). Verified on build
`6382e53` (re-verified on `a65841f`) without any ERP write.

| Check | Result |
|---|---|
| `POST …/connections/CONN-32A904F37FEA/mode` LIVE_WRITE | 200, `LIVE_READ → LIVE_WRITE`, authorised_by "product owner (chat, 2026-09-13)" |
| `/api/health` | `zoho_mode LIVE_WRITE`, `outbound_writes_enabled true`, connections `{ERP: {LIVE_WRITE: 1}}`, gate `is_exactly_1 true` |
| `/organizations` | exactly 60074128927 DEMO WBS; 21 others counted, never named |
| token health | MINTED; `configured_not_in_token: []` (CREATE actually issued) |
| U-REQ (Requestor) emit / drain / mode | 403 / 403 / 403 FORBIDDEN |
| emit on an unknown order | 404 PO_NOT_FOUND |
| emit on a direct Draft order with no request behind it (`PO-EC28CB214677`, ₹1, "Boundary probe (SYNTHETIC, not approved)") | 409 PO_NOT_APPROVED; no outbox row written |
| identical emit on `PO-9EBFF8A906FB` | 202; the same outbox row `OUT-E3803F30C45E`, `created: false` |
| drain | 200, claimed 0, sent 0 — nothing to send, nothing sent |
| served banner | "UAT — SYNTHETIC DATA — ZOHO ERP DEMO TENANT 60074128927 — WRITES ENABLED"; sign-in hint says writes are ENABLED |
| `/`, `/healthz`, `/readyz` | 200 / 200 / 200 (schema 032) |

Not verified here: the integration screen's warning block (client-side render;
needs a signed-in browser session). The JavaScript is syntax-checked.
