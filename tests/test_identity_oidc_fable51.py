"""OpenID Connect protocol half (Stream D): what an id token must survive
before it names anybody, with no database and no network.

A 1024-bit RSA test key is generated once per session from two fixed primes
(deterministic, no third-party library): the tests SIGN tokens with the
private exponent and the module VERIFIES them with the public JWK -- so the
signature check, the algorithm refusal, issuer / audience / expiry / nonce
and the domain policy are each exercised on a real RS256 token.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time

import pytest

from app.backend import identity_oidc as oidc

# Two 512-bit primes (fixed; a TEST key with no life outside this file).
_P = int("1153300138219329453086135637851185963243719096432256689656851391498004735969"
         "4353089098494389049203612660330938939826532257693540326862325236723734031"
         "13", 10)
_Q = int("1219653898135688253587560960936246459581201979262135463719981189625290540957"
         "1443932069758657855447457116560559486836629123468908650359286064492886433"
         "89", 10)


def _is_probable_prime(n: int) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


@pytest.fixture(scope="module")
def key():
    p, q = _P, _Q
    if not (_is_probable_prime(p) and _is_probable_prime(q)):
        # The fixed literals were mistyped: derive two primes deterministically.
        def next_prime(x):
            while not _is_probable_prime(x):
                x += 1
            return x
        p, q = next_prime(_P | 1), next_prime((_Q | 1) + 1000)
    n, e = p * q, 65537
    d = pow(e, -1, (p - 1) * (q - 1))
    return {"n": n, "e": e, "d": d, "kid": "test-key-1"}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwk(key) -> dict:
    return {"kty": "RSA", "kid": key["kid"], "alg": "RS256", "use": "sig",
            "n": _b64(key["n"].to_bytes((key["n"].bit_length() + 7) // 8, "big")),
            "e": _b64(key["e"].to_bytes(3, "big"))}


def _sign(key, header: dict, claims: dict, *, tamper_signature: bool = False) -> str:
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    k = (key["n"].bit_length() + 7) // 8
    em = oidc.emsa_pkcs1_v1_5_encode(signing_input.encode("ascii"), k)
    sig = pow(int.from_bytes(em, "big"), key["d"], key["n"]).to_bytes(k, "big")
    if tamper_signature:
        sig = bytes([sig[0] ^ 0x01]) + sig[1:]
    return f"{signing_input}.{_b64(sig)}"


class _FakeOpener:
    """`urllib.request.urlopen` for the JWKS fetch: a canned document."""

    def __init__(self, doc: dict):
        self.doc, self.calls = doc, 0

    def __call__(self, req, timeout=None):
        self.calls += 1
        body = json.dumps(self.doc).encode()
        outer = self

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return body
        return _Resp()


CFG = oidc.OidcConfig(
    issuer="https://accounts.zoho.in", authorization_endpoint="https://accounts.zoho.in/oauth/v2/auth",
    token_endpoint="https://accounts.zoho.in/oauth/v2/token", jwks_uri="https://accounts.zoho.in/oauth/v2/keys",
    client_id="1000.CLIENT", client_secret="s", redirect_uri="https://uat.example/api/auth/oidc/callback",
    allowed_email_domains=("athagroup.in",))


def _claims(**over) -> dict:
    now = int(time.time())
    base = {"iss": CFG.issuer, "aud": CFG.client_id, "sub": "zoho-user-42", "exp": now + 600,
            "iat": now - 5, "nonce": "nonce-1", "email": "user@athagroup.in", "email_verified": True}
    base.update(over)
    return base


def _jwks(key) -> oidc.JwksCache:
    return oidc.JwksCache(uri=CFG.jwks_uri, opener=_FakeOpener({"keys": [_jwk(key)]}))


_ENDPOINTS = {"CAPEX_OIDC_ISSUER": "https://idp.example/", "CAPEX_OIDC_AUTH_URL": "https://idp.example/auth",
              "CAPEX_OIDC_TOKEN_URL": "https://idp.example/token", "CAPEX_OIDC_JWKS_URL": "https://idp.example/keys"}
_CLIENT = {"CAPEX_OIDC_CLIENT_ID": "c", "CAPEX_OIDC_CLIENT_SECRET": "s",
           "CAPEX_OIDC_REDIRECT_URI": "https://x/cb"}


def test_the_config_names_no_provider_and_is_off_until_every_endpoint_is_set():
    """No provider host lives in `identity_oidc.py` (the adapter rule
    `tests/test_integration_no_hardcoded_endpoints.py` enforces); the owner
    supplies the issuer and its three endpoints, and a client alone -- or
    endpoints alone -- leaves the choice off."""
    cfg = oidc.config_from_env({})
    assert cfg.issuer == "" and cfg.authorization_endpoint == ""
    assert cfg.token_endpoint == "" and cfg.jwks_uri == ""
    assert cfg.enabled is False and cfg.public()["enabled"] is False
    assert "client_secret" not in json.dumps(cfg.public())
    assert oidc.config_from_env(dict(_CLIENT)).enabled is False
    assert oidc.config_from_env(dict(_ENDPOINTS)).enabled is False
    for missing in _ENDPOINTS:
        partial = {**_ENDPOINTS, **_CLIENT}
        del partial[missing]
        assert oidc.config_from_env(partial).enabled is False, missing
    on = oidc.config_from_env({**_ENDPOINTS, **_CLIENT,
                               "CAPEX_OIDC_ALLOWED_EMAIL_DOMAINS": "AthaGroup.in, example.org"})
    assert on.enabled and on.allowed_email_domains == ("athagroup.in", "example.org")
    assert on.issuer == "https://idp.example"  # trailing slash stripped for the `iss` comparison


def test_pkce_and_the_authorization_url():
    verifier = oidc.new_verifier()
    assert 43 <= len(verifier) <= 128
    assert oidc.challenge_for(verifier) == _b64(hashlib.sha256(verifier.encode()).digest())
    url = oidc.authorization_url(CFG, state="st", nonce="nn", verifier=verifier)
    assert url.startswith(CFG.authorization_endpoint + "?")
    assert "code_challenge_method=S256" in url and "state=st" in url and "nonce=nn" in url
    assert "client_secret" not in url and verifier not in url


def test_a_correctly_signed_token_verifies_and_yields_its_claims(key):
    token = _sign(key, {"alg": "RS256", "kid": key["kid"]}, _claims())
    claims = oidc.verify_id_token(token, cfg=CFG, jwks=_jwks(key), nonce="nonce-1")
    assert claims["sub"] == "zoho-user-42" and claims["email"] == "user@athagroup.in"


# The claim overrides are applied when the test RUNS, not when pytest
# collects: a claims dict built at collection carries `exp = now + 600`, and
# twenty minutes into the full suite every such token is simply expired, so
# the check under test is never reached (three cases failed exactly so in the
# 2026-09-13 regression). `_late()` names an offset from the clock at run time.
def _late(**offsets):
    return lambda: {k: int(time.time()) + v for k, v in offsets.items()}


@pytest.mark.parametrize("header, over, tamper, nonce, code", [
    ({"alg": "none", "kid": "test-key-1"}, {}, False, "nonce-1", "OIDC_ALG_REFUSED"),
    ({"alg": "HS256", "kid": "test-key-1"}, {}, False, "nonce-1", "OIDC_ALG_REFUSED"),
    ({"alg": "RS256"}, {}, False, "nonce-1", "OIDC_KID_MISSING"),
    ({"alg": "RS256", "kid": "other"}, {}, False, "nonce-1", "OIDC_KEY_UNKNOWN"),
    ({"alg": "RS256", "kid": "test-key-1"}, {}, True, "nonce-1", "OIDC_SIGNATURE_INVALID"),
    ({"alg": "RS256", "kid": "test-key-1"}, {"iss": "https://accounts.zoho.com"}, False, "nonce-1", "OIDC_ISSUER_MISMATCH"),
    ({"alg": "RS256", "kid": "test-key-1"}, {"aud": "1000.OTHER"}, False, "nonce-1", "OIDC_AUDIENCE_MISMATCH"),
    ({"alg": "RS256", "kid": "test-key-1"}, _late(exp=-1000), False, "nonce-1", "OIDC_TOKEN_EXPIRED"),
    ({"alg": "RS256", "kid": "test-key-1"}, _late(iat=1000), False, "nonce-1", "OIDC_TOKEN_NOT_YET_VALID"),
    ({"alg": "RS256", "kid": "test-key-1"}, {}, False, "nonce-2", "OIDC_NONCE_MISMATCH"),
    ({"alg": "RS256", "kid": "test-key-1"}, {"sub": ""}, False, "nonce-1", "OIDC_SUBJECT_MISSING"),
])
def test_every_check_refuses_by_name(key, header, over, tamper, nonce, code):
    claims = _claims(**(over() if callable(over) else over))
    token = _sign(key, header, claims, tamper_signature=tamper)
    with pytest.raises(oidc.OidcError) as exc:
        oidc.verify_id_token(token, cfg=CFG, jwks=_jwks(key), nonce=nonce)
    assert exc.value.code == code
    assert exc.value.status in (400, 401)


def test_a_tampered_claim_fails_the_signature_not_the_claim_check(key):
    token = _sign(key, {"alg": "RS256", "kid": key["kid"]}, _claims())
    head, body, sig = token.split(".")
    forged = _b64(json.dumps(_claims(sub="somebody-else")).encode())
    with pytest.raises(oidc.OidcError) as exc:
        oidc.verify_id_token(f"{head}.{forged}.{sig}", cfg=CFG, jwks=_jwks(key), nonce="nonce-1")
    assert exc.value.code == "OIDC_SIGNATURE_INVALID"


def test_a_key_rotation_refetches_the_jwks_once(key):
    opener = _FakeOpener({"keys": [_jwk(key)]})
    jwks = oidc.JwksCache(uri=CFG.jwks_uri, opener=opener)
    jwks.refresh()
    assert opener.calls == 1
    assert jwks.get("test-key-1") is not None and opener.calls == 1
    assert jwks.get("unknown") is None and opener.calls == 2


def test_the_domain_policy_and_the_malformed_token():
    assert oidc.email_domain_permitted(CFG, "a@athagroup.in")
    assert oidc.email_domain_permitted(CFG, "a@ATHAGROUP.IN")
    assert not oidc.email_domain_permitted(CFG, "a@gmail.com")
    assert not oidc.email_domain_permitted(CFG, None)
    open_cfg = oidc.OidcConfig(**{**CFG.__dict__, "allowed_email_domains": ()})
    assert oidc.email_domain_permitted(open_cfg, "a@anywhere.example")
    with pytest.raises(oidc.OidcError) as exc:
        oidc.decode_unverified("not.a.jwt.at.all")
    assert exc.value.code == "OIDC_ID_TOKEN_MALFORMED"


def test_the_code_exchange_sends_the_verifier_and_refuses_a_missing_id_token():
    seen: dict = {}

    class _Resp:
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self.body

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["form"] = req.data.decode()
        return _Resp(json.dumps({"access_token": "a", "id_token": "x.y.z"}).encode())

    out = oidc.exchange_code(CFG, code="c1", verifier="v1", opener=opener)
    assert out["id_token"] == "x.y.z" and seen["url"] == CFG.token_endpoint
    assert "code_verifier=v1" in seen["form"] and "client_secret=s" in seen["form"]

    def refusing(req, timeout=None):
        return _Resp(json.dumps({"error": "invalid_grant"}).encode())
    with pytest.raises(oidc.OidcError) as exc:
        oidc.exchange_code(CFG, code="c1", verifier="v1", opener=refusing)
    assert exc.value.code == "OIDC_TOKEN_EXCHANGE_REFUSED" and exc.value.status == 401
    with pytest.raises(oidc.OidcError) as missing:
        oidc.exchange_code(CFG, code="", verifier="v1", opener=opener)
    assert missing.value.code == "OIDC_CODE_MISSING"
