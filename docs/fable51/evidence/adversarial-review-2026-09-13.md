# Adversarial review — streams A to E backend, 2026-09-13

Five independent Sonnet reviewers (one per area) read the commit range
`02bceec..c15df5e`, confirmed suspicions against the local PostgreSQL 16
where they could, and reported findings with file:line, an attack sequence
and a minimal fix. The lead (Fable 5.1) verified each finding against the
code, fixed the ones below in the same session, and re-ran the live suites.
"Status" is the lead's disposition.

| # | Area | Sev | Finding | Status |
|---|---|---|---|---|
| 1 | Internal fulfilment | P0 | `convert_pr_to_po` settled the WHOLE reservation, so a SPLIT/INTERNAL line whose request had not yet ALLOCATED lost its budget hold at conversion (exposure fell by the internal portion; the later allocation was re-checked from scratch). Reproduced live. | **Fixed**: `_external_portions` refuses conversion with `INTERNAL_PORTION_NOT_ALLOCATED` (409) while any internal portion is REQUESTED/APPROVED or has no live request; once allocated the covered rupees are in `internal_allocation_paise` and the settlement releases only the rest. Test: `test_conversion_reads_the_external_portion_and_a_wholly_internal_request_refuses` (hold intact before allocation, 240.00 allocated after conversion). |
| 2 | Internal fulfilment | P1 | Closure's `cwip_balance_paise` was `SUM(actual_paise)` only; a project met wholly from stores capitalised at zero CWIP. Reproduced live. | **Fixed**: `cwip_balance_paise = actual + internal_consumption` in `closure._project_position`; `actual_paise` reported separately. Test asserts `cwip_balance_paise == 300_00` in the export/report/closure scenario. |
| 3 | Internal fulfilment | P2 | `return_material` rounded each leg independently; 0.02+0.49+0.49 of one unit at 101 paise stranded 1 paisa in consumption forever. Reproduced live. | **Fixed**: the leg that clears the site carries exactly what consumption still holds beyond the consumed quantity's value (the close-out `issue` already made). Test: `test_a_return_split_into_legs_strands_no_paisa`. |
| 4 | Admin override | P1 | `budget.approve_revision/approve_transfer` accepted a pre-built override record without binding it to the current maker/actor (only reachable from internal callers today; the write-back builds it correctly); `original_budget.release` checked the actor but not the maker. | **Fixed**: `budget._override_or_refuse` refuses a record naming another actor or maker (`ADMIN_OVERRIDE_INVALID`), `original_budget.release` checks both. Test: a well-formed record produced for another object is refused. |
| 5 | Admin override | P2 | Duplicate `admin_override_reason` field declarations in two `api/closure.py` models (inert). | **Fixed** (deduplicated). |
| 6 | Admin override | P2 | The override reason is length-gated only (10 chars), not content-checked. | Open, accepted: the reason is free text written to the audit trail and mailed to every other Administrator; a content rule is a product decision. |
| 7 | xlsx export | P1 | The workbook is built with a non-streaming `openpyxl.Workbook()` from the whole CSV in memory; no row cap existed, so any `export.create` holder could drive a huge job and OOM the AppSail on download. | **Fixed**: `read_result` refuses a workbook above `exports_xlsx.MAX_ROWS = 20000` rows with `EXPORT_TOO_LARGE_FOR_XLSX` (413) and points at the CSV of the same job. Streaming (`write_only`) rendering is recorded as a post-UAT improvement. |
| 8 | Identity | P1 | A policy/reuse failure on a VALID reset token raised inside the transaction and rolled back the attempt row and audit entry, so a stolen token was an unlimited, unaudited oracle against the password history. Reproduced live. | **Fixed**: policy and reuse are checked BEFORE the token is consumed and their failure is RETURNED (`ok: False`) so the attempt row and `IDENTITY_RESET_REFUSED` commit; the token stays usable for a better password. Test asserts two committed attempt rows and an unconsumed token. |
| 9 | Identity | P2 | PBKDF2 ran only for existing, enabled users: a missing account answered measurably faster. | **Fixed**: `auth.login` and `identity.login` verify against a fixed pair on the missing/disabled branch. |
| 10 | Identity | P2 | No rate limit on authenticated `POST /api/auth/password`; a stolen session could brute-force the current password and lock the owner out. | **Fixed**: `CHANGE:user` bucket (10 / 15 min), refusals returned so attempts persist, `IDENTITY_PASSWORD_CHANGE_REFUSED` audited. Test: the eleventh attempt is `RATE_LIMITED`. |
| 11 | Identity | P2 | Forgot-password does more work for a real account (token insert, outbox row) — a small timing signal. | Open, accepted for UAT: the difference is database round-trips, not CPU; recorded for the production hardening list. |
| 12 | Notifications | P0 | Any Administrator could read a live reset link through `GET /api/notifications/{id}` (body_text) after calling the public forgot route for a victim, then reset the victim's password: full account takeover without mail access. | **Fixed**: `PASSWORD_RESET_REQUESTED` bodies are redacted from the administrator API; a reveal is possible only on a recording-adapter UAT (no mail leaves) with `CAPEX_UAT_REVEAL_RESET_LINKS=1`, and every reveal is audited `NOTIFICATION_BODY_REVEALED`. Test covers redacted / revealed-and-audited / never-when-mail-leaves. |
| 13 | Notifications | P0 | `recipients_for_roles` mailed every holder of a role company-wide; `PR_SUBMITTED` sent plant B's Plant Head plant A's amounts and project — the RLS boundary leaked through mail. | **Fixed**: `recipients_for_roles(..., project_id=)` applies the user's own scope (read_all, restriction and grant tables) in SQL; `PR_SUBMITTED` and `EXCEPTION_RAISED` pass the project. Test: plant B's head receives nothing for plant A's project. |
| 14 | Notifications | P1 | `ADMIN_SELF_APPROVAL_OVERRIDE` (and `EXCEPTION_RAISED`) could be turned off in advance by their recipients. | **Fixed**: both are `MANDATORY_EVENTS`. Test asserts `EVENT_MANDATORY`. |
| 15 | Notifications | P2 | A dispatcher dying between claim and record left a row in `SENDING` forever, invisible to the reclaimer and to retry. | **Fixed**: a `SENDING` row older than ten minutes is reclaimed by `_claim_due` and accepted by `retry_dead`. |

## Controls the reviewers verified as holding

**Internal fulfilment.** The two internal ledger limbs have one writer
(`_RECOMPUTE_DERIVED_SQL`), floored per request before summing, matching
`exports.py` and `reporting.py`; `pr_reserved` derives from `amount -
internal_moved` everywhere, so a rupee moved into an allocation is never also
a hold (exposure held at 1,200.00 through the whole lifecycle in the live
test); maker-checker on approval; one ALLOCATE under concurrent allocates and
no over-issue under concurrent issues (live); idempotency keys scoped per
request; RLS on all three tables in 014's shape with every `scope-exempt`
read keyed on an already-gated id; Auditor's exclusion from `imr.*` is the
documented AUD-C-006 allow-list; cancel refuses stock in the field.

**Admin override.** Role checked before any reason is honoured; a forged
record without the required keys refused; an override never travels with a
delegation (both directions); every accepted override writes its audit entry
in the same transaction; period reopen is not overridable and is enforced by
migration 023's CHECK constraints regardless of role; no header or body can
name the actor or roles; `decide()` derives the permission server-side;
every service reads its object through a scope-gated query before the
override logic runs; PR, IMR and closure re-bind the record to the maker
under the row lock.

**xlsx.** Every string cell is a STRING (`data_type = "s"` after
assignment; verified against openpyxl 3.1.5's writer, which branches on
`data_type` only), headers, filter values and metadata included; the only
formulas are the SUM totals over the counted range; no scope widening between
CSV and xlsx (the workbook is a pure function of the chunk text that already
passed the digest check); the ownership/existence oracle is unchanged.

**Identity.** `alg` pinned to RS256 before anything else; `kid` used only as
a key into the server-configured JWKS with at most one refetch; real
RSASSA-PKCS1-v1_5/SHA-256 with constant-time compare; issuer, audience,
`azp`, `exp`/`iat` within a 120 s skew; nonce per attempt, stored hashed and
compared in constant time; state single-use under `FOR UPDATE` with a
10-minute TTL; PKCE verifier server-side; handoff codes single-use, 60 s,
hash-only, re-checking the session is live; callback redirects only to fixed
fragments; link collisions refused in both directions at the app and by
partial unique indexes; auto-link off by default and exact; no role derived
from a token; break-glass refused on the SSO path; 036 tables reachable only
under the SERVICE principal; the six public routes and nothing else.

**Notifications.** One-statement `FOR UPDATE SKIP LOCKED` claim (no
double-send); dedupe keys namespaced per event/object/version/recipient;
`str.Formatter` substitution never re-parses substituted text and every
seeded template is text-only; mandatory security notices cannot be turned
off; the per-user history is filtered on the caller's own id; the mail layer
reads no credential.

## Method note

Findings were confirmed by the reviewers against a disposable database where
a database was needed; nothing outside the repository was touched. The lead
re-ran the live suites for every reviewed stream after the fixes; counts are
in `docs/FULL_APPLICATION_DELIVERY_STATUS.md`.
