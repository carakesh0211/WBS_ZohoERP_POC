"""Phase 0B Stage 1 — AppSail to PostgreSQL connectivity probe.

Standalone. Contains no CAPEX application code, no client data, and none of the
Phase 0A observability layer — so it carries its own safety rules (see below).

What it answers
---------------
Q-A: can an AppSail container reach an external PostgreSQL and authenticate?

It does NOT answer Q-B. Whether the path can be secured by network controls
needs an authoritative Zoho commitment, a supported private path, or a
client-approved architecture. No amount of sampling from here settles it.

Design constraints, and why
---------------------------
* **Not an SSRF or port-scanning endpoint.** The caller selects a *preconfigured
  endpoint id* — `P1` or `P2` — and nothing else. Hostnames, ports and DSNs are
  never accepted from a request. The endpoint table is built from environment
  variables at start-up and is immutable thereafter.
* **P3 / port 6543 belongs to Stage 2** and is rejected here even if configured.
* **Hard 20 s deadline** on a monotonic clock, checked before each stage. AppSail
  terminates a request at 30 s, and a run that hits the platform cap returns
  nothing — indistinguishable from a hang. This fails itself first and returns
  partial results, marking unrun stages `skipped_deadline`.
* **Stages are independently attributable.** The Phase 0A lesson was that a
  single opaque failure message costs hours. A DNS failure must never be
  confusable with an auth failure.

Safety rules, enforced by construction
--------------------------------------
Never logged, never returned: environment variable values, DSNs, passwords,
tokens, raw exception objects, exception messages, tracebacks.

Exceptions are reduced to `type(exc).__name__` plus an optional driver code
**at the point of capture** — the raw object never travels into a variable that
could reach a response or a log line. This is the same leak class the Phase 0A
review found in tracebacks, and the same remedy.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import platform
import socket
import ssl
import sys
import threading
import time
from typing import Any, Literal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from pydantic import BaseModel  # noqa: E402

# --------------------------------------------------------------- budgets
DEADLINE_S = 20.0
STAGE_BUDGET_S = {"dns": 3.0, "tcp": 4.0, "tls": 4.0, "auth": 5.0, "query": 3.0}
STAGE_ORDER = ("dns", "tcp", "tls", "auth", "query")

# --------------------------------------------------------------- endpoints
#: Built once at start-up from the environment. A request selects a KEY, never
#: a host or port, so this cannot be turned into an SSRF or scanning primitive.
#: P3 (transaction pooler, 6543) is Stage 2 and is deliberately absent.
def _endpoints() -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    if os.environ.get("PGHOST_DIRECT"):
        table["P1"] = {
            "host": os.environ["PGHOST_DIRECT"],
            "port": 5432,
            "endpoint_type": "direct",
            "expected_family": "IPv6",
        }
    if os.environ.get("PGHOST_POOLER"):
        table["P2"] = {
            "host": os.environ["PGHOST_POOLER"],
            "port": 5432,
            "endpoint_type": "session-pooled",
            "expected_family": "IPv4",
        }
    return table


ENDPOINTS = _endpoints()
STAGE2_ONLY = {"P3"}

_probe_lock = threading.Lock()


# --------------------------------------------------------------- safety
def safe_error(exc: BaseException) -> dict[str, str]:
    """Reduce an exception to a publishable classification, at the point of capture.

    The message is discarded here and never stored. Driver and socket messages
    routinely echo host, user and connection parameters; `errno` and pg8000's
    SQLSTATE are safe and are the only detail worth keeping.
    """
    out = {"error_class": type(exc).__name__}
    errno = getattr(exc, "errno", None)
    if isinstance(errno, int):
        out["errno"] = str(errno)
    # pg8000 raises DatabaseError with a dict arg carrying SQLSTATE under 'C'.
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], dict):
        code = args[0].get("C")
        if isinstance(code, str) and code.isalnum():
            out["sqlstate"] = code
    return out


def log(stage: str, outcome: str, ms: float, **safe: Any) -> None:
    """Single-line log. Only values already known to be non-secret reach here."""
    bits = " ".join(f"{k}={v}" for k, v in safe.items())
    print(f"[probe] stage={stage} outcome={outcome} ms={ms:.1f} {bits}", flush=True)


# --------------------------------------------------------------- TLS
# Shared with the build gate; see ca.py for why it is a separate module.
from ca import (  # noqa: E402
    CA_BUNDLE_PATH,
    CaBundleUnusable,
    _ca_summary,
    verified_context,
)

#: Evaluated once at start-up so a packaging fault is visible in the boot log
#: and at /healthz, not first discovered midway through a timed exposure window.
try:
    CA_BUNDLE: dict[str, Any] | None = _ca_summary()
    CA_BUNDLE_ERROR: str | None = None
except CaBundleUnusable as exc:
    CA_BUNDLE, CA_BUNDLE_ERROR = None, str(exc)


# --------------------------------------------------------------- stages
class Deadline:
    def __init__(self, total: float) -> None:
        self._end = time.monotonic() + total

    def remaining(self) -> float:
        return self._end - time.monotonic()

    def allows(self, stage: str) -> bool:
        return self.remaining() >= STAGE_BUDGET_S[stage]


def stage_dns(host: str, port: int, budget: float) -> dict[str, Any]:
    t0 = time.monotonic()
    prev = socket.getdefaulttimeout()
    socket.setdefaulttimeout(budget)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        families = sorted({"IPv6" if i[0] == socket.AF_INET6 else "IPv4" for i in infos})
        addrs = sorted({i[4][0] for i in infos})
        ms = (time.monotonic() - t0) * 1000
        log("dns", "pass", ms, families=",".join(families), n=len(addrs))
        return {"outcome": "pass", "ms": round(ms, 1),
                "address_families": families, "resolved_addresses": addrs}
    except Exception as exc:
        ms = (time.monotonic() - t0) * 1000
        err = safe_error(exc)
        log("dns", "fail", ms, **err)
        return {"outcome": "fail", "ms": round(ms, 1), **err}
    finally:
        socket.setdefaulttimeout(prev)


def stage_tcp(host: str, port: int, budget: float) -> tuple[dict[str, Any], socket.socket | None]:
    t0 = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=budget)
        ms = (time.monotonic() - t0) * 1000
        peer = sock.getpeername()[0]
        family = "IPv6" if sock.family == socket.AF_INET6 else "IPv4"
        log("tcp", "pass", ms, family=family)
        return ({"outcome": "pass", "ms": round(ms, 1),
                 "peer_family": family, "peer_address": peer}, sock)
    except Exception as exc:
        ms = (time.monotonic() - t0) * 1000
        err = safe_error(exc)
        log("tcp", "fail", ms, **err)
        return ({"outcome": "fail", "ms": round(ms, 1), **err}, None)


def stage_tls(sock: socket.socket, host: str, budget: float) -> dict[str, Any]:
    """PostgreSQL SSLRequest, then a VERIFIED wrap.

    SSLRequest is 8 bytes: int32 length 8, int32 code 80877103. The server
    replies with a single byte, 'S' to proceed or 'N' to refuse.
    """
    t0 = time.monotonic()
    try:
        sock.settimeout(budget)
        sock.sendall((8).to_bytes(4, "big") + (80877103).to_bytes(4, "big"))
        reply = sock.recv(1)
        if reply != b"S":
            ms = (time.monotonic() - t0) * 1000
            log("tls", "fail", ms, reason="server_refused_ssl")
            return {"outcome": "fail", "ms": round(ms, 1),
                    "error_class": "SSLRefusedByServer",
                    "server_reply": reply.decode("ascii", "replace")}
        tls = verified_context().wrap_socket(sock, server_hostname=host)
        cert = tls.getpeercert() or {}
        def _name(field):
            return {k: v for part in cert.get(field, ()) for k, v in part}
        ms = (time.monotonic() - t0) * 1000
        log("tls", "pass", ms, proto=tls.version())
        result = {
            "outcome": "pass", "ms": round(ms, 1),
            "protocol": tls.version(),
            "cipher": tls.cipher()[0] if tls.cipher() else None,
            "verified": True,
            "check_hostname": True,
            "sni_sent": host,
            "peer_subject": _name("subject").get("commonName"),
            "peer_issuer": _name("issuer").get("commonName"),
            "not_before": cert.get("notBefore"),
            "not_after": cert.get("notAfter"),
        }
        try:
            tls.close()
        except Exception:  # noqa: BLE001 - closing must never mask the result
            pass
        return result
    except Exception as exc:
        ms = (time.monotonic() - t0) * 1000
        err = safe_error(exc)
        log("tls", "fail", ms, **err)
        return {"outcome": "fail", "ms": round(ms, 1), "verified": False, **err}


def stage_auth_and_query(host: str, port: int, auth_budget: float,
                         query_budget: float) -> tuple[dict, dict]:
    """pg8000 with an explicitly VERIFIED context.

    Whether pg8000 uses the same verified context as the raw handshake is a
    finding in its own right: a verified handshake in the TLS stage does not
    license an unverified one here.
    """
    import pg8000.dbapi

    t0 = time.monotonic()
    ctx = verified_context()
    conn = None
    try:
        conn = pg8000.dbapi.connect(
            host=host, port=port,
            user=os.environ["PGUSER"],
            password=os.environ["PGPASSWORD"],
            database=os.environ["PGDATABASE"],
            ssl_context=ctx,
            timeout=int(auth_budget),
        )
        ms = (time.monotonic() - t0) * 1000
        log("auth", "pass", ms)
        auth = {"outcome": "pass", "ms": round(ms, 1),
                "mechanism": "SCRAM-SHA-256 (server-selected)",
                "used_verified_context": True,
                "context_identical_to_tls_stage": True}
    except Exception as exc:
        ms = (time.monotonic() - t0) * 1000
        err = safe_error(exc)
        log("auth", "fail", ms, **err)
        return ({"outcome": "fail", "ms": round(ms, 1),
                 "used_verified_context": True, **err},
                {"outcome": "not_attempted"})

    t1 = time.monotonic()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1, version(), current_user, inet_client_addr()::text")
        one, version, user, client_addr = cur.fetchone()
        cur.close()
        ms = (time.monotonic() - t1) * 1000
        log("query", "pass", ms)
        query = {"outcome": "pass", "ms": round(ms, 1),
                 "select_1": one, "server_version": version,
                 "current_user": user,
                 "inet_client_addr": client_addr,
                 "inet_client_addr_authoritative": False,
                 "inet_client_addr_note":
                     "Reports the address the SERVER sees. Behind a pooler this "
                     "is the pooler, not the AppSail container. Never treated as "
                     "authoritative for egress identity; see TEST_PLAN.md section 3."}
    except Exception as exc:
        ms = (time.monotonic() - t1) * 1000
        err = safe_error(exc)
        log("query", "fail", ms, **err)
        query = {"outcome": "fail", "ms": round(ms, 1), **err}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return auth, query


# --------------------------------------------------------------- app
app = FastAPI(title="phase-0b-connectivity-probe", version="1.0.0")
# No CORS middleware is installed, deliberately. This endpoint must not be
# callable from a browser origin.


class ProbeRequest(BaseModel):
    """Only an endpoint KEY. No host, port or DSN is accepted from a caller."""
    endpoint: Literal["P1", "P2"]

    model_config = {"extra": "forbid"}


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Public. Touches no database and reveals no configuration."""
    return {"status": "ok", "service": "phase-0b-connectivity-probe",
            "python": platform.python_version(), "machine": platform.machine(),
            # Packaging state only. Reveals no endpoint, credential or token,
            # and the CA is public by construction -- but it lets a broken
            # bundle be caught before a timed window is spent on it.
            "ca_bundle_loaded": CA_BUNDLE is not None,
            "ca_bundle_sha256": CA_BUNDLE["sha256"] if CA_BUNDLE else None,
            "ca_bundle_error": CA_BUNDLE_ERROR}


@app.post("/probe")
def probe(body: ProbeRequest, x_probe_token: str = Header(default="")) -> dict[str, Any]:
    expected = os.environ.get("PROBE_TOKEN", "")
    # Authorisation happens BEFORE any DNS or database activity, and uses a
    # constant-time comparison. The token is a header, never a query parameter,
    # so it cannot leak through logs, referrers or browser history.
    if not expected or not hmac.compare_digest(x_probe_token, expected):
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORISED"})

    # Fail closed. A probe that cannot verify TLS cannot answer Q-A, and must
    # not spend a network round trip implying otherwise. This sits after the
    # 401 so an unauthenticated caller learns nothing about packaging state,
    # and before DNS so nothing leaves the container.
    if CA_BUNDLE is None:
        raise HTTPException(status_code=503, detail={
            "code": "CA_BUNDLE_UNUSABLE", "reason": CA_BUNDLE_ERROR,
            "message": "The pinned CA bundle is unusable. TLS verification is "
                       "never disabled to work around this; rebuild the bundle."})

    key = body.endpoint
    if key in STAGE2_ONLY:
        raise HTTPException(status_code=403, detail={
            "code": "STAGE_2_ENDPOINT",
            "message": "The transaction pooler is Stage 2 and is not authorised."})
    target = ENDPOINTS.get(key)
    if target is None:
        raise HTTPException(status_code=404, detail={
            "code": "ENDPOINT_NOT_CONFIGURED", "endpoint": key})

    if not _probe_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail={
            "code": "PROBE_IN_PROGRESS",
            "message": "One probe runs at a time."})
    try:
        return _run(key, target)
    finally:
        _probe_lock.release()


def _run(key: str, target: dict[str, Any]) -> dict[str, Any]:
    host, port = target["host"], target["port"]
    deadline = Deadline(DEADLINE_S)
    started = time.monotonic()
    stages: dict[str, Any] = {}

    def skip_rest(from_index: int, reason: str) -> None:
        """Record WHY later stages did not run.

        `skipped_deadline` and `skipped_upstream_failure` are different findings:
        the first says the probe ran out of time, the second says an earlier
        stage failed and the rest were never eligible. Collapsing them would
        reintroduce exactly the ambiguity this probe exists to remove.
        """
        for name in STAGE_ORDER[from_index:]:
            stages.setdefault(name, {"outcome": reason})

    sock = None
    try:
        for i, name in enumerate(STAGE_ORDER):
            if not deadline.allows(name):
                log(name, "skipped_deadline", 0.0, remaining=round(deadline.remaining(), 2))
                skip_rest(i, "skipped_deadline")
                break
            budget = min(STAGE_BUDGET_S[name], max(deadline.remaining() - 0.25, 0.1))

            if name == "dns":
                stages["dns"] = stage_dns(host, port, budget)
                if stages["dns"]["outcome"] != "pass":
                    skip_rest(1, "skipped_upstream_failure")
                    break
            elif name == "tcp":
                stages["tcp"], sock = stage_tcp(host, port, budget)
                if stages["tcp"]["outcome"] != "pass":
                    skip_rest(2, "skipped_upstream_failure")
                    break
            elif name == "tls":
                stages["tls"] = stage_tls(sock, host, budget)
                sock = None  # wrapped socket is closed inside the stage
                if stages["tls"]["outcome"] != "pass":
                    skip_rest(3, "skipped_upstream_failure")
                    break
            elif name == "auth":
                q_budget = min(STAGE_BUDGET_S["query"], max(deadline.remaining() - 0.25, 0.1))
                stages["auth"], stages["query"] = stage_auth_and_query(
                    host, port, budget, q_budget)
                break  # auth and query run together on one connection
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass

    for name in STAGE_ORDER:
        stages.setdefault(name, {"outcome": "not_attempted"})

    total_ms = (time.monotonic() - started) * 1000
    return {
        "probe": key,
        "endpoint_type": target["endpoint_type"],
        "port": port,
        "host_suffix": host.split(".", 1)[-1] if "." in host else "n/a",
        "stages": stages,
        "total_ms": round(total_ms, 1),
        "deadline_s": DEADLINE_S,
        "deadline_exceeded": total_ms / 1000 > DEADLINE_S,
        "ca_bundle": {
            "sha256": CA_BUNDLE["sha256"],
            "subjects": [c["subject_cn"] for c in CA_BUNDLE["certificates"]],
            "issuers": [c["issuer_cn"] for c in CA_BUNDLE["certificates"]],
            "not_after": [c["not_after"] for c in CA_BUNDLE["certificates"]],
        } if CA_BUNDLE else None,
        "q_a": "pass" if stages["query"].get("outcome") == "pass" else "fail",
        "q_b": "unresolved",
        "q_b_note":
            "Q-B cannot be established from this run. It requires an authoritative "
            "Zoho static IP/CIDR commitment, a supported private-network path, or "
            "a client-approved security architecture. Sampled address stability is "
            "a hypothesis, never proof.",
    }


if __name__ == "__main__":
    port = int(os.environ.get("X_ZOHO_CATALYST_LISTEN_PORT", "9000"))
    print(f"[probe] python {platform.python_version()} {platform.machine()}", flush=True)
    print(f"[probe] endpoints configured: {sorted(ENDPOINTS)}", flush=True)
    print(f"[probe] ca-bundle: "
          f"{'sha256=' + CA_BUNDLE['sha256'][:16] if CA_BUNDLE else 'UNUSABLE ' + str(CA_BUNDLE_ERROR)}",
          flush=True)
    print(f"[probe] binding 0.0.0.0:{port}", flush=True)
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
