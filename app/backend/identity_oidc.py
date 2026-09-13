"""OpenID Connect for "Continue with Zoho" -- the protocol half (Stream D).

Pure: no database, no clock of its own, no network except through the
`opener` a caller injects (production passes `urllib.request.urlopen`; tests
pass a fake). The persistence and the session are in `pg/identity.py`.

WHAT IS VERIFIED BEFORE AN ID TOKEN NAMES ANYBODY
=================================================

Authorization code flow with PKCE (S256), a server-side confidential client
(the client secret never reaches the browser), and on the way back:

* `state` -- the round trip we started (looked up server-side, single use,
  ten minutes);
* the code exchange at the token endpoint with the `code_verifier` the
  challenge was derived from;
* the id token's SIGNATURE -- RS256 against the provider's JWKS, verified
  here with RSASSA-PKCS1-v1_5 over SHA-256 implemented on the integers
  (`pow(sig, e, n)` against the EMSA-PKCS1-v1_5 encoding of the digest),
  because this deployment vendors no cryptography wheel and a token whose
  signature is not checked is a claim, not an identity;
* `iss` equals the configured issuer, `aud` contains the client id, `exp`
  is in the future, `iat` is not in the future beyond skew, `nonce` equals
  the one we stored (hashed) for this state, `alg` is RS256 and nothing else
  (`none` and HS* are refused by name);
* `sub` is present and non-empty: it is the identity. `email` is recorded
  and checked against the domain policy; it is never the identity.

ZOHO. The endpoints are CONFIGURED, with the India data-centre values
recorded as the defaults the owner verifies (`accounts.zoho.in`); nothing is
invented at run time, and discovery (`/.well-known/openid-configuration`)
is used only when `CAPEX_OIDC_DISCOVERY_URL` is set.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

PROVIDER = "zoho"
STATE_TTL_SECONDS = 600
HANDOFF_TTL_SECONDS = 60
CLOCK_SKEW_SECONDS = 120

#: EMSA-PKCS1-v1_5 DigestInfo prefix for SHA-256 (RFC 8017 §9.2, note 1).
_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


class OidcError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    allowed_email_domains: tuple[str, ...] = ()
    auto_link_by_email: bool = False
    userinfo_endpoint: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri
                    and self.issuer and self.authorization_endpoint
                    and self.token_endpoint and self.jwks_uri)

    def public(self) -> dict[str, Any]:
        """What the sign-in page may know. No secret, no endpoints."""
        return {"provider": PROVIDER, "enabled": self.enabled,
                "label": "Continue with Zoho",
                "allowed_email_domains": list(self.allowed_email_domains)}


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip().lower() for v in (value or "").split(",") if v.strip())


def config_from_env(env: Mapping[str, str] | None = None) -> OidcConfig:
    """Zoho Accounts India as the DEFAULT endpoints; every one overridable."""
    env = os.environ if env is None else env
    issuer = env.get("CAPEX_OIDC_ISSUER", "https://accounts.zoho.in").rstrip("/")
    return OidcConfig(
        issuer=issuer,
        authorization_endpoint=env.get("CAPEX_OIDC_AUTH_URL", f"{issuer}/oauth/v2/auth"),
        token_endpoint=env.get("CAPEX_OIDC_TOKEN_URL", f"{issuer}/oauth/v2/token"),
        jwks_uri=env.get("CAPEX_OIDC_JWKS_URL", f"{issuer}/oauth/v2/keys"),
        userinfo_endpoint=env.get("CAPEX_OIDC_USERINFO_URL") or None,
        client_id=env.get("CAPEX_OIDC_CLIENT_ID", "").strip(),
        client_secret=env.get("CAPEX_OIDC_CLIENT_SECRET", ""),
        redirect_uri=env.get("CAPEX_OIDC_REDIRECT_URI", "").strip(),
        scopes=tuple(env.get("CAPEX_OIDC_SCOPES", "openid email profile").split()),
        allowed_email_domains=_split(env.get("CAPEX_OIDC_ALLOWED_EMAIL_DOMAINS")),
        auto_link_by_email=env.get("CAPEX_OIDC_AUTO_LINK_BY_EMAIL", "0").strip() == "1",
    )


# ----------------------------------------------------------------- PKCE, state
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def new_verifier() -> str:
    return b64url(secrets.token_bytes(48))


def challenge_for(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_state() -> str:
    return b64url(secrets.token_bytes(32))


def new_nonce() -> str:
    return b64url(secrets.token_bytes(24))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def authorization_url(cfg: OidcConfig, *, state: str, nonce: str, verifier: str) -> str:
    params = {
        "response_type": "code", "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri, "scope": " ".join(cfg.scopes),
        "state": state, "nonce": nonce,
        "code_challenge": challenge_for(verifier), "code_challenge_method": "S256",
        "access_type": "online", "prompt": "consent",
    }
    return f"{cfg.authorization_endpoint}?{urllib.parse.urlencode(params)}"


# -------------------------------------------------------------- token exchange
Opener = Callable[..., Any]


def _post_form(opener: Opener, url: str, form: Mapping[str, str], *, timeout: float = 15.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    with opener(req, timeout=timeout) as resp:
        raw = resp.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise OidcError("OIDC_TOKEN_RESPONSE_INVALID",
                        "the token endpoint did not answer with JSON", status=502) from exc


def _get_json(opener: Opener, url: str, *, timeout: float = 15.0) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with opener(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def exchange_code(cfg: OidcConfig, *, code: str, verifier: str, opener: Opener) -> dict[str, Any]:
    """The code for the tokens. The client secret goes in the form body, to
    the token endpoint, and nowhere else."""
    if not code:
        raise OidcError("OIDC_CODE_MISSING", "the provider returned no code")
    try:
        doc = _post_form(opener, cfg.token_endpoint, {
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": cfg.redirect_uri, "client_id": cfg.client_id,
            "client_secret": cfg.client_secret, "code_verifier": verifier})
    except OidcError:
        raise
    except Exception as exc:
        raise OidcError("OIDC_TOKEN_EXCHANGE_FAILED",
                        f"the token endpoint could not be reached ({type(exc).__name__})",
                        status=502) from exc
    if "error" in doc or "id_token" not in doc:
        raise OidcError("OIDC_TOKEN_EXCHANGE_REFUSED",
                        f"the provider refused the code exchange ({doc.get('error', 'no id_token')})",
                        status=401)
    return doc


# ------------------------------------------------------------------- JWKS, RSA
@dataclass
class JwksCache:
    """The provider's keys, fetched once and refreshed when an unknown `kid`
    appears (a rotation) -- at most once per verification."""

    uri: str
    opener: Opener
    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    fetched_at: float = 0.0

    def refresh(self) -> None:
        doc = _get_json(self.opener, self.uri)
        self.keys = {k["kid"]: k for k in doc.get("keys", []) if k.get("kid")}
        self.fetched_at = time.time()

    def get(self, kid: str) -> dict[str, Any] | None:
        if kid not in self.keys:
            self.refresh()
        return self.keys.get(kid)


def _int_from_b64url(text: str) -> int:
    return int.from_bytes(b64url_decode(text), "big")


def rsa_pkcs1v15_sha256_verify(*, n: int, e: int, message: bytes, signature: bytes) -> bool:
    """RSASSA-PKCS1-v1_5 verification (RFC 8017 §8.2.2) on the integers."""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    t = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    if k < len(t) + 11:
        return False
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(em, expected)


def emsa_pkcs1_v1_5_encode(message: bytes, k: int) -> bytes:
    """The encoding a SIGNER applies (used by tests to mint tokens with a
    known private exponent); the verifier above recomputes it."""
    t = _SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def decode_unverified(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = token.split(".")
    if len(parts) != 3:
        raise OidcError("OIDC_ID_TOKEN_MALFORMED", "the id token is not a JWS compact serialisation")
    try:
        header = json.loads(b64url_decode(parts[0]))
        claims = json.loads(b64url_decode(parts[1]))
        signature = b64url_decode(parts[2])
    except (ValueError, TypeError) as exc:
        raise OidcError("OIDC_ID_TOKEN_MALFORMED", "the id token could not be decoded") from exc
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    return header, claims, signing_input, signature


def verify_id_token(token: str, *, cfg: OidcConfig, jwks: JwksCache, nonce: str,
                    now: float | None = None) -> dict[str, Any]:
    """The claims, or a refusal naming the first check that failed."""
    moment = time.time() if now is None else now
    header, claims, signing_input, signature = decode_unverified(token)
    alg = header.get("alg")
    if alg != "RS256":
        raise OidcError("OIDC_ALG_REFUSED", f"id token alg {alg!r} is not RS256", status=401)
    kid = header.get("kid")
    if not kid:
        raise OidcError("OIDC_KID_MISSING", "id token names no key id", status=401)
    jwk = jwks.get(kid)
    if jwk is None or jwk.get("kty") != "RSA":
        raise OidcError("OIDC_KEY_UNKNOWN", f"no RSA key {kid!r} in the provider's JWKS", status=401)
    if not rsa_pkcs1v15_sha256_verify(
            n=_int_from_b64url(jwk["n"]), e=_int_from_b64url(jwk["e"]),
            message=signing_input, signature=signature):
        raise OidcError("OIDC_SIGNATURE_INVALID", "the id token's signature does not verify", status=401)
    if claims.get("iss") != cfg.issuer:
        raise OidcError("OIDC_ISSUER_MISMATCH", "the id token was not issued by the configured issuer", status=401)
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if cfg.client_id not in audiences:
        raise OidcError("OIDC_AUDIENCE_MISMATCH", "the id token is not for this client", status=401)
    if len(audiences) > 1 and claims.get("azp") not in (None, cfg.client_id):
        raise OidcError("OIDC_AUDIENCE_MISMATCH", "the id token's authorized party is not this client", status=401)
    try:
        exp, iat = float(claims["exp"]), float(claims.get("iat", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise OidcError("OIDC_EXP_MISSING", "the id token carries no usable exp", status=401) from exc
    if exp + CLOCK_SKEW_SECONDS < moment:
        raise OidcError("OIDC_TOKEN_EXPIRED", "the id token has expired", status=401)
    if iat and iat - CLOCK_SKEW_SECONDS > moment:
        raise OidcError("OIDC_TOKEN_NOT_YET_VALID", "the id token was issued in the future", status=401)
    if not claims.get("nonce") or not hmac.compare_digest(str(claims["nonce"]), nonce):
        raise OidcError("OIDC_NONCE_MISMATCH", "the id token's nonce is not the one this sign-in started with", status=401)
    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise OidcError("OIDC_SUBJECT_MISSING", "the id token names no subject", status=401)
    return claims


def email_domain_permitted(cfg: OidcConfig, email: str | None) -> bool:
    if not cfg.allowed_email_domains:
        return True
    domain = (email or "").rpartition("@")[2].lower()
    return bool(domain) and domain in cfg.allowed_email_domains
