# `ca-bundle.pem` — provenance record

**Status: ABSENT, deliberately, pending attempt 3.**

Attempt 2's certificate was obtained and verified correctly (full record at §3,
retained as evidence) — but from project `lttaytjmkqsfhgyskamx`, which has been
deleted. Provenance rule 1 requires the anchor to come from **the project
actually under test**, so that file has been removed from the tree rather than
carried forward.

Keeping it would have let the build gate silently accept a stale anchor for a
different database. With it gone the gate fails closed on `ca_bundle_missing`
until attempt 3's own certificate is downloaded — the rule enforcing itself
instead of relying on someone remembering it.

`praktiq` was not opened, at any point, by anyone.

## Why this file exists

Supabase's PostgreSQL endpoints present a certificate chain rooted in a
certificate authority that is **not** in the system trust store. A client
verifying against system CAs therefore fails — correctly — with
`SSLCertVerificationError`.

The first Stage 1 attempt shipped no CA bundle and, worse, degraded silently to
the system store when the file was absent. The failure appeared against the live
endpoint and was indistinguishable from a Catalyst egress restriction or a
network fault. That cost an entire timed exposure window.

Both halves are now fixed: the bundle is a **required build artefact**, and a
missing or unusable one **fails closed** rather than falling back
(`ca.py::CaBundleUnusable`, proven by `test_ca_bundle.py`).

## Correction of record — a claim that was not supported

An earlier revision of this document asserted that `prod-ca-2021.crt` is
"Supabase's shared production root, the same file for every project", and
concluded it could therefore be fetched from any project in advance.

**That was an inference from a filename, not a documented fact, and it is
withdrawn.** Supabase's documentation says the certificate is downloaded from
Database Settings for *your database*. It does not state that the file is
identical across projects, and a shared-looking filename is not evidence that it
is. The same error class as reading "no documentation of static egress IPs" as
"no static egress IPs": absence of a contrary statement is not a guarantee.

The practical consequence was worse than the wording: it would have had the
trust anchor sourced from a project that is out of scope.

## Q6 is not a blocker

Zoho support question 6 asks whether a custom CA bundle may be shipped inside a
deployment bundle. **That question does not gate this work.** AppSail has already
accepted a deployment ZIP containing vendored files and executed from it — the
Phase 0A spike and the first Stage 1 deployment both did exactly that. Whether a
`.pem` alongside `main.py` loads is therefore an **empirically testable property
of the deployment**, and it is tested by deploying it.

Q6 remains worth asking for written confirmation, and Q3 remains genuinely
decisive — but for **Q-B**, not for Q-A. See `../TEST_PLAN.md` §1.

## 1. Approved provenance rule

Binding. Every clause is a refusal as much as an instruction.

| # | Rule |
|---|---|
| 1 | The CA must come from **the exact newly created throwaway project** used for the connectivity test |
| 2 | It must be downloaded from **that project's** Database Settings → SSL Configuration |
| 3 | The **project reference is recorded before** the certificate is downloaded |
| 4 | Record the certificate **SHA-256, subject, issuer, validity window and download time** |
| 5 | **Never** trust a certificate obtained from a TLS handshake or a third-party repository |
| 6 | **Never** open or inspect `praktiq`, or any other pre-existing project |

Rule 5 is not pedantry. Taking a root from the handshake it is meant to
authenticate is trust-on-first-use, which is precisely the property pinning
exists to remove. Rule 1 is what rules 2–4 are *for*: the recorded fingerprint
means something only if it is tied to the database actually under test.

| | |
|---|---|
| Documented filename | `prod-ca-2021.crt` (as named in Supabase's documentation) |
| Source of record | Supabase Dashboard → **the throwaway project** → Database Settings → SSL Configuration |
| Documentation | `https://supabase.com/docs/guides/platform/ssl-enforcement` |
| Published direct URL | **None.** No download URL is documented; the dashboard is the only source of record |
| Identical across projects? | **Unknown, and not assumed.** Not documented either way |

## 2. When it is obtained

**Inside the exposure window, immediately after project creation** — not before,
because before the project exists there is no in-scope source for it.

This is a deliberate trade. Sourcing the certificate correctly costs exposure
minutes that pre-fetching would have saved, and it means packaging is finished
under the clock rather than ahead of it. Provenance integrity wins; see
`../OPERATOR_RUNBOOK.md` for the sequence and its consequences, including the
abort rule if the bundle fails to load on AppSail.

## 3. Recorded provenance

**Attempt 2 (project now deleted).** Retained as evidence of what was obtained
and verified; **not** a valid anchor for attempt 3, which must re-download.

```
project name:       wbs-phase0b-connectivity-spike-0830
project reference:  lttaytjmkqsfhgyskamx        <- recorded 20:55 BEFORE download, per rule 3
downloaded from:    that project's Database Settings -> SSL configuration
                    (link target: supabase-downloads S3 /prod/ssl/prod-ca-2021.crt)
downloaded at:      2026-08-30 21:03:08 local  (2026-08-30 15:33:08 UTC)
downloaded by:      the operator, manually, from the dashboard
saved as:           prod-ca-2021.crt, copied byte-identical to ca-bundle.pem
sha256:             700723581420dd1ac98fd7e9ac529f0ef210eadcaf87fc868a3ad7d114c2f3b7
subject CN:         Supabase Root 2021 CA
subject O:          Supabase Inc
issuer CN:          Supabase Root 2021 CA        (self-signed root)
not before:         Apr 28 10:56:53 2021 GMT
not after:          Apr 26 10:56:53 2031 GMT
currently valid:    yes
bundle sha256:      51160f270898b67f2c453380f12baccd8eaf6145583964bf151bacdd1d2d62ef
                    (480 files, 3,694,808 bytes)
```

**Observation, recorded without generalising.** The download control on this
project's own settings page points at a shared Supabase S3 path
(`/prod/ssl/prod-ca-2021.crt`), and the certificate is a self-signed root named
"Supabase Root 2021 CA". That is consistent with the file being common across
projects -- but it is **still not a documented guarantee**, and the withdrawn
claim is not revived. What is recorded is what the rule requires: this file was
obtained from `lttaytjmkqsfhgyskamx`'s own Database Settings, and it is pinned
as the anchor for that project alone. `praktiq` was not opened, by anyone.

**Committing the certificate is intentional.** It is a public root certificate,
contains no secret, no credential and no connection string, and committing it
makes the trust anchor reviewable, diffable and pinned — the same reasoning that
puts a lockfile in a repository.

It is committed **as the anchor used for one specific throwaway project**, and
carries no claim of validity for any other project. Phase 1 must re-derive its
own from whatever database it actually uses.

## 4. What is asserted about it

| Property | Where enforced |
|---|---|
| Present, non-empty, parseable, at least one CA certificate | `ca.py::_ca_summary` |
| At least one certificate currently within its validity window | `ca.py::_ca_summary` |
| Absent / empty / malformed / expired → **fail closed**, never system-CA fallback | `test_ca_bundle.py` |
| It is the **only** trust anchor in the context | `test_ca_bundle.py::test_the_pinned_bundle_is_the_only_trust_anchor` |
| Hostname verification on, `CERT_REQUIRED`, TLS ≥ 1.2 | `test_ca_bundle.py` |
| Verification cannot be disabled anywhere in the probe | `test_ca_bundle.py` (source scan over `main.py`, `ca.py`, `build_bundle.py`) |
| Present at the archive root, byte-identical to the validated file | `build_bundle.py::verify_zip` |
| The **deployed** service loaded this exact fingerprint | `/healthz` → `ca_bundle_sha256`, checked before the role is created |
| Certificate **bytes** never returned or logged | `test_ca_bundle.py::test_the_summary_never_returns_certificate_bytes` |

The `/healthz` row is what makes rule 1 verifiable end to end: the fingerprint
recorded at download must equal the fingerprint the running service reports.

## 5. Expiry is an operational obligation

The certificate has a finite validity window. As it approaches expiry the build
gate begins failing on `ca_bundle_expired` — deliberately, and before a live
connection breaks. Re-download from the source of record for **the project then
in use**, re-run the gate, and update §3 in the same commit.
