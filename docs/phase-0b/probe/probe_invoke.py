"""Operator-assisted probe invocation for Phase 0B Stage 1.

Why this exists
---------------
The first attempt stalled because setting `PGPASSWORD` through the Catalyst
Console dialog would have written the password into an automation transcript.
The rule that produced that stall is correct and is not being relaxed; what
changes is that the token now never has to be *given* to anyone in order to be
*used*.

The operator types the probe token at a prompt. It is read with `getpass`, so
it is not echoed to the terminal, not recorded in shell history, and not passed
as an argv element where `ps` could read it. It exists only as a header on two
outbound requests and is never written to disk.

What is written
---------------
Sanitised evidence only, one file per endpoint. Hostnames and resolved
addresses are redacted by default: the probe's own answer to Q-A is a
stage-by-stage verdict, and the target's IP addresses are not part of it.

Usage
-----
    python probe_invoke.py --base-url https://<service-url> --out evidence/
    python probe_invoke.py --self-test          # sanitiser proof, no network
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

ENDPOINTS = ("P1", "P2")
TIMEOUT_S = 40  # above the probe's own 20 s deadline, so we see ITS verdict

#: Redacted unless --keep-addresses is passed. These identify the target host
#: rather than answering Q-A, and the target is a throwaway that is deleted
#: minutes later; there is no reason to commit them.
ADDRESS_KEYS = {"resolved_addresses", "peer_address", "sni_sent", "host_suffix"}

#: Never written under any flag. `inet_client_addr` is deliberately NOT here:
#: it is our egress address as the server saw it, which is Q-B evidence.
ALWAYS_STRIP = {"password", "token", "dsn", "connection_string", "secret"}


def sanitise(obj, keep_addresses: bool = False, secrets: tuple[str, ...] = ()):
    """Recursively strip target-identifying and secret-shaped values.

    Three layers, because the cost of one being wrong is a credential in Git:

    1. ``secrets`` -- literal values the caller KNOWS, removed wherever they
       appear, including inside free prose. Key-name matching alone missed
       exactly this case, which the self-test caught before any live use.
    2. key-name matching, for fields we can anticipate;
    3. value-shape matching, for addresses and hostnames.

    Layer 1 is the only one that can catch a secret embedded in an unexpected
    string, and it only works for values we hold. The probe is separately built
    never to emit the database password, which this helper never receives.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(tok in lk for tok in ALWAYS_STRIP):
                out[k] = "[REDACTED]"
            elif k in ADDRESS_KEYS and not keep_addresses:
                if isinstance(v, list):
                    out[k] = f"[REDACTED: {len(v)} address(es)]"
                else:
                    out[k] = "[REDACTED]"
            else:
                out[k] = sanitise(v, keep_addresses, secrets)
        return out
    if isinstance(obj, list):
        return [sanitise(v, keep_addresses, secrets) for v in obj]
    if isinstance(obj, str):
        for secret in secrets:
            if secret and secret in obj:
                obj = obj.replace(secret, "[REDACTED]")
        # Defence in depth: a bare IPv4/IPv6 literal or a supabase hostname
        # appearing in some field we did not anticipate.
        s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "[IPv4]", obj)
        s = re.sub(r"\b[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){3,7}\b", "[IPv6]", s, flags=re.I)
        s = re.sub(r"\b[\w.-]*\.supabase\.(?:co|com)\b", "[HOST]", s, flags=re.I)
        return s
    return obj


def call(base_url: str, endpoint: str, token: str) -> dict:
    req = urllib.request.Request(
        base_url.rstrip("/") + "/probe",
        data=json.dumps({"endpoint": endpoint}).encode(),
        # The token is a HEADER. Never a query parameter, so it cannot reach a
        # server access log, a referrer or browser history.
        headers={"Content-Type": "application/json", "X-Probe-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return {"http_status": resp.status, "body": json.loads(resp.read())}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {"unparsable_error_body": True}
        return {"http_status": exc.code, "body": body}
    except Exception as exc:
        # Class only. A urllib message embeds the full URL.
        return {"http_status": None, "transport_error": type(exc).__name__}


def health(base_url: str) -> dict:
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/healthz", timeout=20) as r:
            return {"http_status": r.status, "body": json.loads(r.read())}
    except Exception as exc:
        return {"http_status": None, "transport_error": type(exc).__name__}


def self_test() -> int:
    """Prove the sanitiser before it is trusted with a live response."""
    token = "TOKEN-must-never-appear-9Q2mK7vL"
    password = "PASSWORD-must-never-appear-x8Yt"
    sample = {
        "probe": "P1",
        "host_suffix": "exampleproject.supabase.co",
        "stages": {
            "dns": {"outcome": "pass",
                    "resolved_addresses": ["2406:da1a::1", "13.234.1.2"],
                    "address_families": ["IPv6"]},
            "tcp": {"outcome": "pass", "peer_address": "13.234.1.2"},
            "tls": {"outcome": "pass", "sni_sent": "db.exampleproject.supabase.co",
                    "verified": True},
            "query": {"outcome": "pass", "inet_client_addr": "3.7.11.22"},
        },
        "probe_token": token,
        "db_password": password,
        "note": f"connect to db.exampleproject.supabase.co as 13.234.1.2 with {password}",
    }
    clean = json.dumps(sanitise(sample, secrets=(token, password)))
    failures = []
    for secret in (token, password):
        if secret in clean:
            failures.append(f"secret survived sanitisation: {secret[:12]}...")
    for leak in ("supabase.co", "13.234.1.2", "2406:da1a"):
        if leak in clean:
            failures.append(f"address/host survived sanitisation: {leak}")
    if '"verified": true' not in clean.lower():
        failures.append("sanitiser destroyed the evidence it was meant to preserve")
    if "inet_client_addr" not in clean:
        failures.append("inet_client_addr key was dropped; it is Q-B evidence")

    for f in failures:
        print(f"FAIL: {f}", file=sys.stderr)
    if failures:
        return 1
    print("self-test OK: secrets and target addresses redacted, verdicts preserved")
    print(json.dumps(json.loads(clean)["stages"], indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", help="AppSail service URL, no credentials in it")
    ap.add_argument("--out", default="evidence")
    ap.add_argument("--keep-addresses", action="store_true",
                    help="retain resolved addresses in the evidence files")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--token-stdin", action="store_true",
                    help="read the token from stdin instead of prompting -- for "
                         "piping from a password manager, and for testing. Still "
                         "never an argv element.")
    args = ap.parse_args()

    if args.self_test:
        return self_test()
    if not args.base_url:
        ap.error("--base-url is required")
    if "@" in args.base_url or "?" in args.base_url:
        ap.error("--base-url must carry no credentials and no query string")

    # Never an argv element under either path, so `ps` and shell history
    # cannot see it. getpass reads the console directly (which on Windows is
    # why it cannot be piped -- hence --token-stdin for the piped case).
    if args.token_stdin:
        token = sys.stdin.readline().strip()
    else:
        token = getpass.getpass("X-Probe-Token (input hidden): ")
    if not token.strip():
        print("No token entered; nothing was sent.", file=sys.stderr)
        return 2

    os.makedirs(args.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    h = sanitise(health(args.base_url), args.keep_addresses, (token,))
    hb = h.get("body", {}) if isinstance(h.get("body"), dict) else {}
    print(f"/healthz -> {h.get('http_status')} "
          f"ca_bundle_loaded={hb.get('ca_bundle_loaded')} "
          f"endpoints={hb.get('configured_endpoints')} "
          f"loaded_at={hb.get('configuration_loaded_at')}")

    # ---------------------------------------------------------- operator gate
    # Attempt 2 ran the probe against an instance that had started before the
    # environment variables existed. Every call returned 404
    # ENDPOINT_NOT_CONFIGURED, and that 404 was written to the evidence files as
    # though it were a result. It was not: no DNS, TCP, TLS, auth or query stage
    # ever ran. This gate refuses to produce that artefact again.
    gate_failures = []
    if h.get("http_status") != 200:
        gate_failures.append(f"/healthz returned {h.get('http_status')}, expected 200")
    if hb.get("ca_bundle_loaded") is not True:
        gate_failures.append(f"ca_bundle_loaded is {hb.get('ca_bundle_loaded')}, expected true")
    configured = hb.get("configured_endpoints")
    if not isinstance(configured, list):
        gate_failures.append("healthz did not report configured_endpoints -- the deployed "
                             "build predates this gate; redeploy before probing")
    else:
        missing = [e for e in ENDPOINTS if e not in configured]
        if missing:
            gate_failures.append(
                f"endpoint(s) {missing} absent from configured_endpoints={configured}. "
                f"The running instance did not load them. AppSail binds environment "
                f"variables at INSTANCE START, so set every variable first and then "
                f"deploy -- a configuration change does not recycle a live instance.")

    if gate_failures:
        print("\nABORTED before any probe ran:", file=sys.stderr)
        for f in gate_failures:
            print(f"  - {f}", file=sys.stderr)
        print("\nNo evidence file was written. A refusal to run is not a result, "
              "and must not be recorded as one.", file=sys.stderr)
        return 3

    results = {"healthz": h}
    for ep in ENDPOINTS:
        print(f"running {ep} ...", flush=True)
        raw = call(args.base_url, ep, token)
        clean = sanitise(raw, args.keep_addresses, (token,))
        detail = clean.get("body", {}).get("detail") if isinstance(clean.get("body"), dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if code == "ENDPOINT_NOT_CONFIGURED":
            print(f"\nABORTED at {ep}: ENDPOINT_NOT_CONFIGURED.", file=sys.stderr)
            print("  The instance lost or never had this endpoint. No stage ran, so "
                  "there is nothing to record. Redeploy after setting variables.",
                  file=sys.stderr)
            return 3
        results[ep] = clean
        path = os.path.join(args.out, f"{ep}-{stamp}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2, sort_keys=True)
        body = clean.get("body", {})
        stages = body.get("stages", {})
        print(f"  http={clean.get('http_status')} q_a={body.get('q_a')} "
              f"q_b={body.get('q_b')}")
        for name in ("dns", "tcp", "tls", "auth", "query"):
            if name in stages:
                print(f"    {name:<6} {stages[name].get('outcome')}")
        print(f"  written: {path}")

    del token  # not a security control on its own; the point is it is never stored

    summary = os.path.join(args.out, f"summary-{stamp}.json")
    with open(summary, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, sort_keys=True)
    print(f"\nsanitised evidence in {args.out}/ -- review before committing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
