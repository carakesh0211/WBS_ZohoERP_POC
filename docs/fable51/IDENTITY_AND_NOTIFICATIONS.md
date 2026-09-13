# Identity and notifications (Streams D and E, 2026-09-13)

What shipped, how it is configured, and the one-time steps the owner takes
outside the repository. No credential appears here or anywhere in the code.

## Sign-in choices

The sign-in page offers two choices, both served by `app/backend/api/identity.py`:

| Choice | Route(s) | Requires |
|---|---|---|
| Sign in with WBS account | `POST /api/auth/login` (unchanged) | the seeded credential, or the durable one the user set |
| Continue with Zoho | `POST /api/auth/oidc/start` → provider → `GET /api/auth/oidc/callback` → `POST /api/auth/oidc/complete` | `CAPEX_OIDC_*` configured and PostgreSQL |
| Forgot password | `POST /api/auth/forgot`, `POST /api/auth/reset` | PostgreSQL, a mail adapter |
| Change password (signed in) | `POST /api/auth/password` | PostgreSQL |

`GET /api/auth/providers` tells the page which choices are enabled; it never
exposes a secret or an endpoint.

### Continue with Zoho (OpenID Connect)

Authorization code with PKCE (S256), a separate confidential server client.
The identity is the id token's `sub`; e-mail is recorded and checked against
the domain policy, never used as the identity. Verified before anybody is
named: RS256 signature against the provider JWKS (verified on the integers in
`app/backend/identity_oidc.py`; `none` and HS* are refused by name), issuer,
audience (and `azp` when several), `exp`, `iat`, the stored `nonce`, and the
single-use `state`. The subject must be LINKED to an application user
(`identity_external_link`); a link is made by an Administrator
(`POST /api/admin/users/{user_id}/identities`, audited `IDENTITY_LINKED`) or,
only when `CAPEX_OIDC_AUTO_LINK_BY_EMAIL=1`, by a verified e-mail matching
exactly one active PostgreSQL `app_user`. Roles come from the linked WBS
account and from nowhere in the token; an Administrator role is never granted
by this path; the break-glass account never signs in through Zoho. The
session is created server-side and handed to the browser through a
single-use, one-minute code in the URL fragment — no session id in a URL.

Environment. NO provider is written into the code (the adapter rule:
nothing outside the two ERP adapters names a product host, and
`tests/test_integration_no_hardcoded_endpoints.py` fails the build if one
does), so the owner sets the issuer and all three endpoints; the values below
are Zoho Accounts **India**, to be verified against the provider's own
discovery document before enabling. "Continue with Zoho" stays off until
every one of the seven required variables is present:

```
CAPEX_OIDC_ISSUER=https://accounts.zoho.in
CAPEX_OIDC_AUTH_URL=https://accounts.zoho.in/oauth/v2/auth
CAPEX_OIDC_TOKEN_URL=https://accounts.zoho.in/oauth/v2/token
CAPEX_OIDC_JWKS_URL=https://accounts.zoho.in/oauth/v2/keys
CAPEX_OIDC_CLIENT_ID=<the SEPARATE server-based client's id>
CAPEX_OIDC_CLIENT_SECRET=<its secret; Catalyst environment variable only>
CAPEX_OIDC_REDIRECT_URI=https://<uat host>/api/auth/oidc/callback
CAPEX_OIDC_SCOPES="openid email profile"
CAPEX_OIDC_ALLOWED_EMAIL_DOMAINS=athagroup.in
CAPEX_OIDC_AUTO_LINK_BY_EMAIL=0
```

Owner steps: create a **Server-based** client in the Zoho API console for
the India DC with the redirect URI above (not the Self Client used for the
ERP connector); paste its id and secret into the Catalyst environment; link
each tester's Zoho `sub` to their WBS user through the admin route (the
subject is shown on the first refused sign-in's audit entry as its last six
characters only; the full value comes from the tester's Zoho profile or the
first successful sign-in attempt with auto-link on).

### Sign in with WBS account, forgot password

`identity.login` consults the DURABLE credential (`identity_credential`,
PostgreSQL) before the seeded SQLite hash, so a password set through the
reset flow survives an AppSail recycle (the SQLite identity store is a
per-process scratch copy). Forgot password always answers the same generic
sentence; internally it is rate-limited per user (3 / 15 min) and per address
(10 / 15 min) in PostgreSQL, issues a random 32-byte token stored as its
SHA-256 only, valid 15 minutes, single-use, mailed through the outbox with a
deep link `CAPEX_PUBLIC_URL/#reset?token=…`. Reset applies the policy (12+
characters, upper, lower, digit, not the user id), refuses the last five
passwords and the seeded one, revokes every session, audits
`IDENTITY_PASSWORD_CHANGED`, and mails `PASSWORD_CHANGED`. Every refusal is
audited on the user's stream.

Break-glass: `CAPEX_BREAK_GLASS_USER_ID` names one seeded Administrator whose
sign-in is audited `IDENTITY_BREAK_GLASS_LOGIN` and who can never be linked
to or sign in through Zoho. Its credential is in `uat-credentials.json`
(hash only), outside the repository.

## Notifications

`app/backend/pg/notifications.py` over migration 036: a durable outbox, nine
seeded templates, retry with backoff (2^attempts minutes, capped at an hour),
dead-letter, per-user preferences (security notices cannot be turned off),
delivery history, deduplication by key, correlation ids. Sending never
happens inside a business transaction: services `try_enqueue` in their own
transaction; `POST /api/notifications/dispatch` (Administrator) or the ticker
(`CAPEX_NOTIFICATIONS_DISPATCH_SECONDS=60` on AppSail) drains what is due.
Monitoring: `GET /api/notifications` carries counts by state, the oldest due
row and the adapter's availability.

Events wired: `PR_SUBMITTED` (approver roles), `PR_APPROVED` (requester),
`IMR_APPROVED` (requester), `ADMIN_SELF_APPROVAL_OVERRIDE` (every other
System Administrator), `EXPORT_COMPLETED` (requester), `EXCEPTION_RAISED`
(finance roles), `PASSWORD_RESET_REQUESTED`, `PASSWORD_CHANGED`,
`IDENTITY_LINKED`. Recipients are resolved from PostgreSQL `app_user`
e-mails and `role_grant`; a user with no PostgreSQL identity row receives no
mail and the audit entry remains the record.

### Mail provider

`app/backend/integration/mail_adapter.py`; `CAPEX_MAIL_ADAPTER` selects:

* `recording` (default, UAT today): nothing leaves the process; every message
  is a `RECORDED` delivery row. Safe until the sender is verified.
* `catalyst_sdk`: Zoho Catalyst Mail through `zcatalyst_sdk`, which AppSail
  provisions with the project's own credentials (nothing configured here).
  Needs `CAPEX_MAIL_FROM` (a verified sender) and `CAPEX_MAIL_FROM_NAME`.
* `catalyst_rest`: written down, UNVERIFIED, refuses to send.

Owner steps before switching to `catalyst_sdk`: in the Catalyst console
(project `wbs-capex-uat`, India DC) open **Mail → Sender domains**, add the
sending domain, publish the DNS records it shows (SPF, DKIM) at the domain's
registrar, wait for the console to mark the domain **verified**, then set
`CAPEX_MAIL_FROM=<address at that domain>` and `CAPEX_MAIL_ADAPTER=
catalyst_sdk` in the AppSail environment and recycle. Until then the outbox
accumulates `RECORDED` deliveries. The body of a reset mail is REDACTED from
the administrator's outbox API (an Administrator who could read it could
reset any account); on a recording-adapter UAT the owner may set
`CAPEX_UAT_REVEAL_RESET_LINKS=1` so a tester's reset can be completed from
the outbox detail -- every reveal is audited `NOTIFICATION_BODY_REVEALED`,
and the switch is ignored the moment mail actually leaves.

## Tests

`tests/test_identity_oidc_fable51.py` (18, no database), `tests/test_pg_
identity_notifications_fable51.py` (live), plus the matrix pins in
`tests/test_api_auth.py` (recorded in `tests/ADAPTATIONS.md`).
