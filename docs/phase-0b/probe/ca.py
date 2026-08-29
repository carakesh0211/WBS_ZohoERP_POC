"""Pinned certificate-authority handling for the Phase 0B probe.

A separate, **stdlib-only** module on purpose. The build gate
(`build_bundle.py`) and the running probe must apply byte-identical CA rules;
if the gate reimplemented them, the two could drift and the gate would start
certifying something the runtime does not do. Keeping this importable without
FastAPI or the vendored tree is what makes sharing it possible.
"""
from __future__ import annotations

import hashlib
import os
import ssl
import time
from typing import Any

CA_BUNDLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ca-bundle.pem")


class CaBundleUnusable(RuntimeError):
    """The pinned CA bundle is missing, empty, malformed or wholly expired.

    Raised INSTEAD of falling back to the system trust store.

    The first build of this probe wrote ``if os.path.isfile(bundle):`` and
    silently degraded to system CAs when the file was absent. Supabase presents
    its own CA, so the omission surfaced as a verification failure against the
    live endpoint and was indistinguishable from a platform or network problem.
    It cost an entire test window. Failing closed means a packaging mistake can
    only ever look like a packaging mistake.

    Carries a fixed reason token and never a path, a filename or certificate
    bytes, so it is safe to return and to log.
    """


def _ca_summary(path: str | None = None) -> dict[str, Any]:
    """Validate the pinned bundle and describe it.

    Returns provenance-grade metadata -- file digest, subject, issuer, validity
    window. Never returns or logs certificate BYTES: the fingerprint identifies
    the file, and the body adds nothing an auditor needs.
    """
    target = CA_BUNDLE_PATH if path is None else path
    if not os.path.isfile(target):
        raise CaBundleUnusable("ca_bundle_missing")
    with open(target, "rb") as fh:
        raw = fh.read()
    if not raw.strip():
        raise CaBundleUnusable("ca_bundle_empty")
    digest = hashlib.sha256(raw).hexdigest()

    inspector = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        inspector.load_verify_locations(cafile=target)
    except Exception:
        # `from None` deliberately: OpenSSL's message embeds the file path.
        raise CaBundleUnusable("ca_bundle_malformed") from None
    certs = inspector.get_ca_certs()
    if not certs:
        raise CaBundleUnusable("ca_bundle_no_ca_certificates")

    now = time.time()
    described: list[dict[str, Any]] = []
    live = 0
    for cert in certs:
        def field(name: str, _c: dict = cert) -> dict[str, str]:
            return {k: v for part in _c.get(name, ()) for k, v in part}
        not_after = cert.get("notAfter")
        try:
            expires_at = ssl.cert_time_to_seconds(not_after)
        except Exception:
            raise CaBundleUnusable("ca_bundle_unparseable_validity") from None
        current = expires_at > now
        live += int(current)
        described.append({
            "subject_cn": field("subject").get("commonName"),
            "subject_o": field("subject").get("organizationName"),
            "issuer_cn": field("issuer").get("commonName"),
            "not_before": cert.get("notBefore"),
            "not_after": not_after,
            "currently_valid": current,
        })
    if not live:
        raise CaBundleUnusable("ca_bundle_expired")
    return {"sha256": digest, "certificate_count": len(certs),
            "valid_certificate_count": live, "certificates": described}


def verified_context(path: str | None = None) -> ssl.SSLContext:
    """Chain validation against the PINNED bundle, hostname verification, SNI, TLS 1.2+.

    Negotiating encryption is not sufficient: an unverified session proves only
    that something answered.

    Passing ``cafile`` to :func:`ssl.create_default_context` suppresses
    ``load_default_certs()``, so the pinned CA is the ONLY trust anchor -- the
    system store is not a silent second chance.
    """
    target = CA_BUNDLE_PATH if path is None else path
    _ca_summary(target)  # fails closed before any context exists
    ctx = ssl.create_default_context(cafile=target)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
