# Runbook — credential rotation

**Status: `UNVERIFIED`.** No credential has ever been rotated in this build.
There is no managed PostgreSQL, no secret store and no live Zoho tenant here.
This is a design, and must not be presented as a working procedure.

## What is rotated, and what breaks if it is done wrongly

| Credential | Held as | Rotating it wrongly causes |
| --- | --- | --- |
| PostgreSQL application role password | `CAPEX_DB_URL` / secret ref | Every request fails at once; `/readyz` goes 503 while `/healthz` stays 200 — which is the point of them being separate |
| Zoho OAuth refresh token | `refresh_token_ref` on `zoho_connection` | Outbound emission stalls, circuit opens, DLQ grows. **Not** data loss — the queue is durable |
| Zoho client secret | `client_secret_ref` | As above, plus every connection sharing the client |
| Application session signing key | secret ref | Every logged-in user is signed out |

Note the shape of the `zoho_connection` columns: `client_id_ref`,
`client_secret_ref` and `refresh_token_ref` are **references**
(`secretref://...`), not values. Nothing in this runbook involves reading a
secret into a shell.

## Order of operations, and the rule underneath it

**Add the new credential before removing the old one.** Every step below is
written so there is a window in which both are valid. A rotation with no overlap
is an outage with a rotation in it.

### PostgreSQL role password

1. Create the new password in the secret store as a **new version**. Do not
   overwrite.
2. `ALTER ROLE capex_app PASSWORD '<new>'` — PostgreSQL has one password per
   role, so this is the one step with no overlap. Schedule it in a maintenance
   window. Existing sessions authenticated once and are unaffected; new
   connections fail until step 3 lands.
3. Point the application's secret ref at the new version; restart, or let the
   pool recycle.
4. Confirm `/readyz` returns 200. **`/healthz` is not evidence** — it never
   touches the database.
5. Retire the old secret version only after a full pool recycle.

Because step 2 has no overlap, prefer a **two-role** rotation where the platform
allows it: create `capex_app_2`, grant it identically, cut over, then drop the
first. The single-role path above is the fallback, not the preference.

### Zoho OAuth refresh token

1. Re-consent in the Zoho console and capture the new refresh token.
2. Write it as a new secret version.
3. Update `refresh_token_ref` on the connection row.
4. Force a token refresh and confirm `oauth_status`.
5. **Check the circuit first.** A rotation performed while a circuit is open
   looks exactly like a rotation that failed. Confirm the circuit state before
   concluding the new token is bad.
6. Retire the old version.

## After any rotation

* Confirm `/readyz` is 200 and `/healthz` is 200. Both, and for different
  reasons.
* Check DLQ depth. A rotation that failed silently shows up there first.
* Confirm the rotation is in the audit log with the actor who performed it.
  **A rotation that leaves no audit entry is indistinguishable from a
  compromise.**
* Do not close the change until a request has actually succeeded through the new
  credential. "The config was updated" is not evidence.

## What is NOT decided

Rotation cadence, who holds the secret-store master credential, whether rotation
is automated, and the break-glass path for a credential compromised outside a
maintenance window. All are prerequisites, and none exists.
