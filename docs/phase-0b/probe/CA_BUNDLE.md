# `ca-bundle.pem` — provenance record

**Status: NOT YET PRESENT.** The file is required by the build gate, and the
build fails without it. Obtaining it is a manual operator step (§2 below),
because it is not published at any URL Supabase documents.

## Why this file exists

Supabase's PostgreSQL endpoints present a certificate chain rooted in **their
own certificate authority**, not a public root in the system trust store. A
client that verifies against system CAs therefore fails — correctly — with
`SSLCertVerificationError`.

The first Stage 1 attempt shipped no CA bundle and, worse, degraded silently to
the system store when the file was absent. The failure appeared against the live
endpoint and was indistinguishable from a Catalyst egress restriction or a
network fault. That cost an entire timed exposure window.

Both halves are now fixed: the bundle is a **required build artefact**, and a
missing or unusable one **fails closed** rather than falling back
(`ca.py::CaBundleUnusable`, proven by `test_ca_bundle.py`).

## Q6 is not a blocker

Zoho support question 6 asks whether a custom CA bundle may be shipped inside a
deployment bundle. **That question does not gate this work.** AppSail has already
accepted a deployment ZIP containing vendored files and executed from it — the
Phase 0A spike and the first Stage 1 deployment both did exactly that. Whether a
`.pem` alongside `main.py` loads is therefore an **empirically testable property
of the deployment**, and it is tested by deploying it.

Q6 remains worth asking for written confirmation, and Q3 remains genuinely
decisive — but for **Q-B**, not for Q-A. See `../TEST_PLAN.md` §1.

## 1. Source of record

| | |
|---|---|
| Certificate | Supabase production root CA |
| Documented filename | `prod-ca-2021.crt` |
| Official source | Supabase Dashboard → Project → **Database Settings → SSL Configuration** |
| Documentation | `https://supabase.com/docs/guides/platform/ssl-enforcement` |
| Published direct URL | **None.** No download URL is documented; the dashboard is the only source of record |

Only an official Supabase source is acceptable. A certificate scraped from a
live TLS handshake is **not** acceptable provenance: trusting a root because the
server offered it is trust-on-first-use, which defeats the point of pinning.

## 2. The manual step

`ca-bundle.pem` is **not** project-specific — it is Supabase's shared production
root, the same file for every project. So it can be obtained **once, in advance**,
and does not have to be fetched inside the exposure window. That decoupling is
deliberate: the bundle must be built, deployed and health-checked *before* any
throwaway project exists, so the 60-minute clock is spent on the probe rather
than on packaging.

**Operator steps**

1. Open the Supabase dashboard for a project you own and choose
   **Database Settings → SSL Configuration**.
2. Download the certificate (`prod-ca-2021.crt`).
3. Save it, unmodified, as:
   `docs/phase-0b/probe/ca-bundle.pem`
4. Run the gate:

```bash
python docs/phase-0b/probe/build_bundle.py --verify-ca-only
```

It prints the SHA-256, subject, issuer and validity window, and exits non-zero
if the file is missing, empty, malformed, expired, or contains no CA
certificate.

5. Paste that output into §3 below and commit both files.

> **Boundary note.** The pre-existing project `praktiq` is out of scope for this
> engagement and I will not open it. If you choose to take the certificate from
> that project's settings page, that is your decision as its owner; the file is
> a public root certificate and carries no project data, no credential and no
> connection string. The alternative is to download it from the new throwaway
> project once it is created — at the cost of doing packaging work inside the
> exposure window, which is what this section exists to avoid.

## 3. Recorded provenance

Filled in from `build_bundle.py --verify-ca-only` once the file is in place.

```
sha256:        <pending>
subject CN:    <pending>
subject O:     <pending>
issuer CN:     <pending>
not before:    <pending>
not after:     <pending>
downloaded:    <pending, UTC>
downloaded by: <pending, named person>
```

**Committing the certificate is intentional.** It is a public root CA, contains
no secret, and committing it makes the trust anchor reviewable, diffable and
pinned — the same reasoning that puts a lockfile in a repository.

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
| Certificate **bytes** never returned or logged | `test_ca_bundle.py::test_the_summary_never_returns_certificate_bytes` |

## 5. Expiry is an operational obligation

`prod-ca-2021.crt` has a finite validity window. When it approaches expiry the
build gate begins failing on `ca_bundle_expired` — deliberately, and before a
live connection breaks. Re-download from the same source of record, re-run the
gate, and update §3 in the same commit.
