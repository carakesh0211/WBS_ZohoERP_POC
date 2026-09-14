# UAT owner enablement — 2026-09-14

Everything the application can do without a secret is done and proven below. Each remaining step is one the
owner performs, with the exact action prepared. No secret value appears here, is retrieved, transmitted or
committed; every `<…>` is a value the owner holds. Deployed application: `63b07c6` (deploy record 15),
https://wbs-capex-uat-50045784768.development.catalystappsail.in, schema 036.

## A. Continue with Zoho (OpenID Connect) — AWAITING OWNER INPUT

### A1. The client to create (Zoho API console, India DC)

| Field | Value |
|---|---|
| Console | https://api-console.zoho.in (the DC the UAT users' Zoho accounts live in; the ERP tenant is IN) |
| Client type | **Server-based Applications** (a confidential client; NOT the Self Client used for the ERP connector) |
| Client name | `WBS CAPEX UAT sign-in` (any name) |
| Homepage URL | `https://wbs-capex-uat-50045784768.development.catalystappsail.in` |
| Authorized Redirect URI | `https://wbs-capex-uat-50045784768.development.catalystappsail.in/api/auth/oidc/callback` — exactly this, one entry, no trailing slash |

The console shows a Client ID and a Client Secret once. The secret goes into the Catalyst console only.

### A2. The seven required variables (Catalyst console → project `wbs-capex-uat-fable51` → AppSail `wbs-capex-uat` → environment variables → then recycle)

| Variable | Value | Source |
|---|---|---|
| `CAPEX_OIDC_ISSUER` | the `issuer` in Zoho's discovery document, expected `https://accounts.zoho.in` | https://accounts.zoho.in/.well-known/openid-configuration — read it; the id token's `iss` must equal this string exactly |
| `CAPEX_OIDC_AUTH_URL` | `authorization_endpoint` from the same document, expected `https://accounts.zoho.in/oauth/v2/auth` | discovery document |
| `CAPEX_OIDC_TOKEN_URL` | `token_endpoint`, expected `https://accounts.zoho.in/oauth/v2/token` | discovery document |
| `CAPEX_OIDC_JWKS_URL` | `jwks_uri`, expected `https://accounts.zoho.in/oauth/v2/keys` | discovery document |
| `CAPEX_OIDC_CLIENT_ID` | `<client id>` | API console |
| `CAPEX_OIDC_CLIENT_SECRET` | `<client secret>` | API console — console only, never in a file, chat or commit |
| `CAPEX_OIDC_REDIRECT_URI` | `https://wbs-capex-uat-50045784768.development.catalystappsail.in/api/auth/oidc/callback` | this document |

Optional, with their defaults: `CAPEX_OIDC_SCOPES` (`openid email profile`), `CAPEX_OIDC_ALLOWED_EMAIL_DOMAINS`
(empty = no domain restriction; set e.g. `athagroup.in` to refuse other domains),
`CAPEX_OIDC_AUTO_LINK_BY_EMAIL` (`0` — keep it `0`; see A4). No provider host is written into the code
(`app/backend/identity_oidc.py::config_from_env`, enforced by `tests/test_integration_no_hardcoded_endpoints.py`).

### A3. What is verified in the implementation (read on 2026-09-14, and pinned by `tests/test_identity_oidc_fable51.py`, 18 cases)

| Property | Where |
|---|---|
| Local WBS sign-in stays available whatever OIDC does | `POST /api/auth/login` unchanged; `GET /api/auth/providers` on UAT today: `local.enabled: true`, `forgot_password: true` |
| Zoho choice shown only when every required variable is present | `OidcConfig.enabled` requires issuer, all three endpoints, client id, client secret and redirect URI; UAT today reports `oidc.enabled: false` and the button is hidden |
| PKCE S256 | `identity_oidc.py:154` — verifier 48 random bytes, challenge S256, verifier kept server-side with the state |
| State single-use, nonce bound | `pg/identity.py` `_consume_state` (consumed once; a replay is `OIDC_STATE_INVALID`); the nonce is stored as a SHA-256 and compared before the token is even verified (`identity.py:499-500`) |
| Signature | RS256 only (`alg` `none`/HS\* refused by name), `kid` required, key looked up in the provider JWKS, verified on the integers (`identity_oidc.py:276-287`) |
| Issuer, audience, `azp`, `exp`, `iat` | `identity_oidc.py:288-303`, ±`CLOCK_SKEW_SECONDS` |
| No role comes from the token | roles are read from the LINKED WBS user's `role_grant`; the token contributes only `sub`, `email`, `email_verified` (`pg/identity.py` completion, "an Administrator role is never granted by this path") |
| Unlinked identity refused AND audited | `OIDC_NOT_LINKED` 403 + audit `IDENTITY_SSO_REFUSED` on the `(unmapped)` stream carrying the subject's last six characters (`identity.py:525-531`) |
| Auto-link by e-mail is opt-in | only when `CAPEX_OIDC_AUTO_LINK_BY_EMAIL=1`, `email_verified` is true, and exactly ONE active `app_user` carries that e-mail; otherwise refused as above (`identity.py:516-522`) |
| Break-glass account never signs in through Zoho | `OIDC_BREAK_GLASS_REFUSED` (`identity.py:538-541`) |
| Session hand-off | one-minute single-use code in the URL fragment, exchanged by `POST /api/auth/oidc/complete`; no session id in a URL |

### A4. Linking each tester's Zoho subject to the EXISTING WBS user (no user is created)

The subject is Zoho's `sub` claim for that person. Two ways to obtain it without guessing: the tester signs
in once (refused `OIDC_NOT_LINKED`; the audit entry shows the last six characters, enough to confirm which
person), or read it from Zoho's userinfo for that account. Then, signed in as an Administrator (`X-Session`
from a normal WBS sign-in):

```bash
curl -sS -X POST "https://wbs-capex-uat-50045784768.development.catalystappsail.in/api/admin/users/U-RAKESH/identities" \
  -H "Content-Type: application/json" -H "X-Session: <administrator session id>" \
  -d '{"subject": "<zoho sub of that person>", "email": "<their zoho e-mail, optional>"}'
```

Repeat for `U-PRITHA`, `U-SURAJ`, `U-ABHISHEK` (and any other tester) with THEIR subject. The route refuses a
subject already linked elsewhere, audits `IDENTITY_LINKED`, and notifies the user; `GET` on the same path
lists links, `DELETE` removes one (audited `IDENTITY_UNLINKED`). Duplicate users are impossible by this path:
it links to a `user_id` that must already exist.

## B. Local sign-in and password reset — PROVEN on UAT (synthetic tester), reveal switch AWAITING OWNER DECISION

Proven on 2026-09-14 with the synthetic tester `U-REQ`: `POST /api/auth/forgot` → 202 with the generic
sentence, and the same 202 and sentence for a user id that does not exist; one
`PASSWORD_RESET_REQUESTED` outbox row for the tester; an Administrator's `GET /api/notifications/{id}`
returned the row with its body REDACTED (61 characters, no token); `POST /api/notifications/dispatch`
moved it to `SENT` with one `RECORDED` delivery row (recording adapter — nothing left the platform); the
second and third dispatch claimed nothing. No token appears in any evidence file.

Token properties, from `app/backend/pg/identity.py`: 32 random bytes, stored as SHA-256 only, single use,
valid 15 minutes, every refusal audited, all sessions revoked on a successful reset.

**Recommendation:** set `CAPEX_UAT_REVEAL_RESET_LINKS=1` on the UAT AppSail while the adapter is
`recording`. Without it no tester can finish "Forgot password", because the only copy of the link is in a
mail that never leaves. With it: the body is revealed ONLY through `GET /api/notifications/{id}`, which
carries the `admin.reset` permission dependency (Administrator), every reveal writes an
`NOTIFICATION_BODY_REVEALED` audit entry naming the Administrator and the notification
(`pg/notifications.py:389-411`), and `reveal_permitted()` returns false the moment `CAPEX_MAIL_ADAPTER` is
anything but `recording`, so the switch cannot survive into real mail. Remove the variable when a verified
sender exists.

## C. E-mail — RECORDING ONLY

The application on UAT reports adapter `recording`, sender `None`; nothing has left the platform and
nothing is claimed to. The connector's read-only listing of the project's mail domains and e-mail
configurations failed on 2026-09-14 (`CatalystbyZoho_List_All_Domains` / `…_Emails_Configuration`: the
connector answered "Error while executing meta-tool" on three attempts), so readiness was NOT inspected
from here; the owner inspects it in the console. No other Catalyst project was touched.

Checklist, in order (Catalyst console → `wbs-capex-uat-fable51`, id 4239000000144371, Development):

1. **Mail → Sender domains → Add domain**: the domain the sender address will use (e.g. `athagroup.in`).
2. Publish the records the console shows at the domain's DNS: the SPF `TXT` and the DKIM record (the
   console gives the exact host names and values). Wait for the console to show **Verified**.
3. **Mail → Email configuration**: add the sender address at that domain (e.g. `wbs-uat@<domain>`).
4. AppSail environment: `CAPEX_MAIL_FROM=<that address>`, `CAPEX_MAIL_FROM_NAME=WBS CAPEX UAT`,
   `CAPEX_MAIL_ADAPTER=catalyst_sdk`; remove `CAPEX_UAT_REVEAL_RESET_LINKS`; recycle.
5. Prove it: trigger ONE synthetic notification (e.g. a `POST /api/auth/forgot` for a synthetic tester whose
   e-mail the owner controls), `POST /api/notifications/dispatch` as an Administrator, and read the delivery
   row: outcome `SENT`, provider `catalyst_sdk`, a provider message id, and the mail received. Only then is
   e-mail "working"; until then it is recording-only.

Creation stays transactional, dispatch stays outside the financial transaction: services `try_enqueue`
inside their own transaction; `dispatch_due` claims, sends and records in three separate transactions,
none held open across the provider call (`pg/notifications.py::dispatch_due`).

## D. Operational settings

### D1. `CAPEX_BREAK_GLASS_USER_ID` — AWAITING OWNER SELECTION

Eligible existing Administrators on UAT (every one authenticates today, role smoke 13/13): `U-ADM` (the
seeded synthetic Administrator), `U-RAKESH`, `U-PRITHA`, `U-SURAJ`, `U-ABHISHEK` (the four ERP-linked
people). Five are eligible, so no id is chosen here. Considerations: the break-glass account can never be
linked to or sign in through Zoho and its every sign-in is audited `IDENTITY_BREAK_GLASS_LOGIN`; it should
be an account whose password the owner keeps offline and that nobody uses day to day — that argues for
`U-ADM` over a person's account, but the choice is the owner's. Set `CAPEX_BREAK_GLASS_USER_ID=<chosen id>`
and recycle.

### D2. `CAPEX_NOTIFICATIONS_DISPATCH_SECONDS=60` — RECOMMENDED, not yet set

Without it the outbox is drained only by an Administrator's `POST /api/notifications/dispatch` (which is
how today's proof was done). Delivery semantics, read from `pg/notifications.py` and proven on UAT:

* claim is one atomic statement (`FOR UPDATE SKIP LOCKED`, state → `SENDING`, `attempts + 1`), so two
  tickers or a ticker plus a manual dispatch never claim the same row;
* each attempt writes its own `notification_delivery` row; `dedupe_key` is UNIQUE, so the same event for
  the same recipient is never enqueued twice;
* after a restart, `QUEUED` and due `FAILED` rows are dispatched once; a row a crash left in `SENDING` is
  reclaimed after 10 minutes (`STALE_SENDING`). Delivery is therefore **at-least-once**: a duplicate is
  possible only if the process dies after the provider accepted the mail and before the delivery row is
  written — a ten-minute window per crash, visible as two delivery rows. With the recording adapter there
  is no external effect at all.
* UAT 2026-09-14: three queued rows → one dispatch → three `SENT`, one delivery row each; a second and a
  third dispatch claimed 0.

### D3. Audit-anchor Cron Function — EXTERNAL BLOCKER (job token; cloud resource creation)

`GET /api/audit/verify` on UAT reports `NEVER_ANCHORED`: the verification is intact and unchanged; no
anchor has ever been written because the scheduler does not exist. Nothing here weakens the verification.
The function and runbook exist (`catalyst/functions/capex_audit_anchor/`, `docs/runbooks/audit-anchor.md`).
It is blocked on (a) a job token that does not exist anywhere under `~/.capex-tools`, and (b) the creation
of a Function and a Cron in the Catalyst project, which is a cloud-resource creation this session does not
perform without separate authorisation. Owner steps, exact:

1. Generate the token offline, ≥ 32 characters, keep it only in the console: e.g.
   `python -c "import secrets; print(secrets.token_urlsafe(48))"` on the owner's machine.
2. Build the function folder (no network, secrets refused):
   `python tools/appsail/build_anchor_function.py --wheels ~/.capex-tools/appsail-staging/wheels --out ~/.capex-tools/appsail-out/capex_audit_anchor`
3. Deploy it to project 4239000000144371 as a **Cron Function** (`catalyst deploy --only functions:capex_audit_anchor`
   from a project directory bound to that project, or the console upload).
4. Function environment: `CAPEX_ANCHOR_JOB_TOKEN=<the token>` plus the same `CAPEX_DB_*` set the AppSail
   uses (host, port, name `capex_tmpl_uat`, user, password, `CAPEX_DB_SSLMODE=verify-full`,
   `CAPEX_DB_SSLROOTCERT` to the bundled CA). The function presents and checks the token from its own
   environment (`hmac.compare_digest`, minimum 32 characters, no default).
5. Job Scheduling → Cron `capex-audit-anchor-daily`, type Cron Function, daily `02:15` UTC, target
   `capex_audit_anchor`, timeout 900 s.
6. Submit it once by hand; then `GET /api/audit/verify` must report `anchored: true`, `state` not
   `NEVER_ANCHORED`. Exit codes and what each means: runbook §3.
