"""Structured logging, correlation and RED metrics.

Remediates AUD-M-007: "No durable structured log sink, metrics or alerting."

Three things live here.

1. **Correlation.** ``main.py`` already mints a correlation id per request and
   echoes it in ``X-Correlation-Id``, but nothing carried it any further. A
   ``ContextVar`` now propagates it, so one id traces a request through the
   service layer into ``audit_log.correlation_id`` - and, once the integration
   platform lands, on through job, inbox, outbox and integration_event.

2. **Redaction.** Log records are structured, so a caller can attach fields.
   That is a leak risk. Every record is filtered through the classification
   rules before it is emitted. This is the log-side half of the data
   classification; the storage-side half is column encryption and masking.

   Note the deliberate asymmetry with plan section 10.4: values are **removed**
   from logs, never hashed. Hashing a GSTIN into a log adds no operational
   value and creates a correlatable pseudo-identifier - worse than omitting it.

3. **RED metrics.** Rate, Errors, Duration per route. In-process counters, read
   by a scrape endpoint. Deliberately not a metrics library: AppSail instances
   are killed after five minutes of uptime, so any in-process aggregate is a
   short-lived sample, not a source of truth. The durable record is the log
   stream; counters exist for a liveness view only. Anything that must survive
   an instance - watermarks, quotas, circuit state - belongs in PostgreSQL.

No test in this module touches the network, and importing it has no side effect
beyond registering a formatter.
"""
from __future__ import annotations

import json
import logging
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

# --------------------------------------------------------------- correlation
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_actor: ContextVar[str | None] = ContextVar("actor", default=None)


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_actor(user_id: str | None) -> None:
    _actor.set(user_id)


def get_actor() -> str | None:
    return _actor.get()


@contextmanager
def correlation(cid: str | None, actor: str | None = None) -> Iterator[None]:
    """Bind a correlation id (and optionally an actor) for a block of work."""
    c_token = _correlation_id.set(cid)
    a_token = _actor.set(actor) if actor is not None else None
    try:
        yield
    finally:
        _correlation_id.reset(c_token)
        if a_token is not None:
            _actor.reset(a_token)


# --------------------------------------------------------------- redaction
#: Field names whose values must never reach a log sink.
#: Sourced from the data classification in plan section 10.4.
SENSITIVE_FIELDS = frozenset({
    # credentials and tokens
    "password", "password_hash", "password_salt", "secret", "client_secret",
    "refresh_token", "access_token", "authorization", "session_id", "x_session",
    "api_key", "token", "idem_key", "cookie", "set_cookie",
    # regulated identifiers - REMOVED from logs, never hashed
    "gst_no", "gstin", "pan_no", "pan", "tax_id_number",
    # personal data
    "email", "phone", "mobile", "contact_email", "contact_phone",
    # restricted
    "bank_account", "account_number", "ifsc", "iban", "swift",
})

REDACTED = "[REDACTED]"

#: Bearer-style tokens appearing inside free text.
_TOKEN_IN_TEXT = re.compile(
    r"(Zoho-oauthtoken|Bearer|Basic)\s+[A-Za-z0-9._\-+/=]+", re.IGNORECASE
)


#: Value-shaped patterns, for secrets that arrive inside free text or under a
#: benign key. Key-name matching alone is not enough: an exception message, a
#: log format argument, or a list of header pairs all carry values with no key.
_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Zoho / HTTP authorisation headers
    (re.compile(r"(Zoho-oauthtoken|Bearer|Basic)\s+[A-Za-z0-9._\-+/=]+", re.I), r"\1 " + REDACTED),
    # key=value or key: value pairs naming a sensitive field in free text
    (re.compile(
        r"\b((?:client[_-]?secret|refresh[_-]?token|access[_-]?token|api[_-]?key|"
        r"password|gstin|gst[_-]?no|pan[_-]?no)\s*[=:]\s*)\S+", re.I,
    ), r"\1" + REDACTED),
    # Indian GSTIN: 2 digits, 5 letters, 4 digits, letter, digit/letter, Z, digit/letter
    (re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b"), REDACTED),
    # Indian PAN: 5 letters, 4 digits, 1 letter
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), REDACTED),
)


#: Single words that make a field name sensitive wherever they appear.
#: Matched WHOLE-WORD against the tokenised key, so `pan` matches `panNumber`
#: and `pan_no` but not `panel`; `secret` matches `clientSecret` but not
#: `secretariat`. Erring toward over-redaction is the correct direction here.
_SENSITIVE_WORDS = frozenset({
    "password", "secret", "token", "tokens", "apikey", "authorization", "auth",
    "session", "cookie", "credential", "credentials",
    "gst", "gstin", "gstno", "pan", "panno", "taxid",
    "email", "phone", "mobile",
    "iban", "ifsc", "swift",
})

#: Multi-word concepts, matched against the fully-joined key.
_SENSITIVE_JOINED = frozenset({
    "apikey", "clientsecret", "refreshtoken", "accesstoken", "sessionid",
    "passwordhash", "passwordsalt", "idemkey", "setcookie",
    "bankaccount", "accountnumber", "taxidnumber",
    "contactemail", "contactphone", "vendoremail",
})


def _key_tokens(key: Any) -> tuple[list[str], str]:
    """Split a field name into words, plus its fully-joined form.

    Handles snake_case, kebab-case, camelCase and HTTP header style, because
    Zoho payloads and HTTP headers use all four. Exact lowercase matching
    against a fixed list missed every variant and was the single largest class
    of redaction bypass found in adversarial review.
    """
    raw = str(key)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw)      # camelCase split
    words = [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]
    return words, "".join(words)


def _is_sensitive_key(key: Any) -> bool:
    words, joined = _key_tokens(key)
    if joined in _SENSITIVE_JOINED or joined in _SENSITIVE_WORDS:
        return True
    if any(w in _SENSITIVE_WORDS for w in words):
        return True
    # Drop a leading single-letter segment, so `X-Api-Key` -> `apikey`.
    if words and len(words[0]) == 1 and "".join(words[1:]) in _SENSITIVE_JOINED:
        return True
    # Adjacent word pairs, so `account_number` and `bank_account` match.
    return any(
        words[i] + words[i + 1] in _SENSITIVE_JOINED for i in range(len(words) - 1)
    )


def scrub_text(text: str) -> str:
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(key: Any, value: Any) -> Any:
    if _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return redact(value)
    if isinstance(value, (list, tuple, set)):
        return [redact_value(key, v) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    # Anything else - bytes, dataclasses, Pydantic models, objects whose repr
    # carries a credential - is stringified HERE, before scrubbing. Leaving it
    # for json.dumps(default=str) let it bypass redaction entirely.
    return scrub_text(str(value))


def redact(payload: dict) -> dict:
    return {k: redact_value(k, v) for k, v in payload.items()}


# --------------------------------------------------------------- formatter
_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()) | {
    "message", "asctime", "taskName",
}


#: Root keys the formatter owns exclusively. A caller must never be able to
#: overwrite these - a log line claiming level=INFO for an error, or carrying a
#: forged correlation_id or actor, is worse than no log line, because it is
#: evidence that reads as trustworthy and is not.
PROTECTED_ROOT_KEYS = frozenset({
    "ts", "level", "logger", "message", "correlation_id", "actor",
    "exception", "fields",
})


class StructuredFormatter(logging.Formatter):
    """One JSON object per line, with correlation and redaction applied.

    Caller-supplied ``extra`` fields are nested under ``fields`` rather than
    merged into the root object. Merging let a caller shadow any root key -
    ``log.info("x", extra={"level": "DEBUG", "actor": "someone-else"})`` would
    have produced a record that misreports its own severity and attributes the
    action to the wrong user. In a system whose audit trail is the deliverable,
    that is a forgeable-evidence problem, not a formatting nicety.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        cid = get_correlation_id()
        if cid:
            payload["correlation_id"] = cid
        actor = get_actor()
        if actor:
            payload["actor"] = actor
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            payload["fields"] = redact(extras)
        if record.exc_info:
            # Scrubbed like everything else. An unredacted traceback is the
            # highest-probability real leak in this system: an HTTP client
            # exception routinely echoes the request headers that carried the
            # bearer token.
            payload["exception"] = scrub_text(self.formatException(record.exc_info))
        # The message has already had its %-args interpolated by getMessage(),
        # so a secret passed as a lazy format argument is inside this string.
        payload["message"] = scrub_text(payload["message"])
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure(level: int = logging.INFO, stream=None) -> logging.Logger:
    """Install the structured formatter on the application logger.

    Idempotent - safe to call from both the AppSail entrypoint and a Function.
    """
    logger = logging.getLogger("capex")
    logger.setLevel(level)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(StructuredFormatter())
    logger.addHandler(handler)
    return logger


log = logging.getLogger("capex")


# --------------------------------------------------------------- RED metrics
class Metrics:
    """Rate, Errors, Duration per route.

    In-process and therefore short-lived: an AppSail instance is killed after
    five minutes of uptime, so these are a liveness sample rather than a durable
    record. Anything that must survive an instance goes to PostgreSQL.
    """

    def __init__(self) -> None:
        self.requests: dict[tuple[str, str], int] = {}
        self.errors: dict[tuple[str, str], int] = {}
        self.duration_ms: dict[tuple[str, str], list[float]] = {}

    #: Retained samples per route. Latency percentiles do not need more, and an
    #: unbounded list is a memory leak in a long-lived process.
    MAX_SAMPLES = 512

    #: Distinct route labels retained. A bounded cap is essential because the
    #: label must never be attacker-controlled; see `observe`.
    MAX_ROUTES = 256

    def observe(self, method: str, route: str, status: int, ms: float) -> None:
        """Record one request.

        `route` MUST be a route TEMPLATE ("/api/projects/{project_id}/wbs"),
        never a concrete path. Labelling by concrete path gives one series per
        id, which is unbounded cardinality - and on an unauthenticated endpoint
        it is a memory-exhaustion vector, since any caller can mint new labels
        by varying the URL. `main.py` resolves the template and passes OTHER
        for anything unmatched.
        """
        key = (method, route)
        if key not in self.requests and len(self.requests) >= self.MAX_ROUTES:
            key = (method, "OVERFLOW")
        self.requests[key] = self.requests.get(key, 0) + 1
        if status >= 500:
            self.errors[key] = self.errors.get(key, 0) + 1
        samples = self.duration_ms.setdefault(key, [])
        if len(samples) < self.MAX_SAMPLES:
            samples.append(ms)
        else:
            # Reservoir-style overwrite: keep a bounded, still-representative
            # window rather than only the first MAX_SAMPLES requests.
            samples[self.requests[key] % self.MAX_SAMPLES] = ms

    @staticmethod
    def _pct(values: list[float], q: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        idx = min(int(q * len(s)), len(s) - 1)
        return round(s[idx], 2)

    def snapshot(self) -> dict:
        out = []
        for key, count in sorted(self.requests.items(), key=lambda kv: str(kv[0])):
            method, route = key
            d = self.duration_ms.get(key, [])
            out.append({
                "method": method,
                "route": route,
                "requests": count,
                "errors": self.errors.get(key, 0),
                "p50_ms": self._pct(d, 0.50),
                "p95_ms": self._pct(d, 0.95),
                "p99_ms": self._pct(d, 0.99),
            })
        return {
            "note": (
                "In-process counters only. AppSail instances are killed after "
                "5 minutes of uptime, so these are a liveness sample, not a "
                "durable metric store."
            ),
            "routes": out,
        }

    def reset(self) -> None:
        self.requests.clear()
        self.errors.clear()
        self.duration_ms.clear()


metrics = Metrics()


# --------------------------------------------------------------- alerts
#: Conditions that must page a human. Wired to the sink in Phase 1.
ALERT_CONDITIONS = {
    "LEDGER_DIVERGENCE": "P1 - materialised control cell disagrees with the derived ledger",
    "AUDIT_CHAIN_BROKEN": "P1 - verify_audit_chain reported intact=False",
    "CIRCUIT_OPEN": "P2 - a Zoho connection circuit opened",
    "DLQ_DEPTH": "P2 - dead-letter queue above threshold",
    "DAILY_QUOTA_EXHAUSTED": "P2 - Zoho daily API ceiling reached",
    "JOB_RESUME_LIMIT": "P2 - a job exceeded max_resume_count",
}


def alert(condition: str, detail: str, **fields: Any) -> None:
    """Emit an alert-classified log record.

    Deliberately fails loudly on an unknown condition: an alert nobody has
    defined a response for is not an alert, and silently accepting one would
    let a P1 be raised with no runbook behind it.
    """
    if condition not in ALERT_CONDITIONS:
        raise ValueError(
            f"Unknown alert condition {condition!r}. Add it to ALERT_CONDITIONS "
            "with a severity and a runbook entry before raising it."
        )
    # Caller fields are nested rather than splatted. Python's logging refuses
    # any `extra` key that collides with a LogRecord attribute - and `module`,
    # `name`, `args`, `filename` and `process` all collide. `module` in
    # particular is used throughout this codebase for the Zoho sync module, so
    # the natural call alert("DLQ_DEPTH", ..., module="Vendors") would raise
    # KeyError instead of alerting. An alert path that throws is worse than
    # none, so the fields go in a namespace of their own.
    log.error(
        detail,
        extra={
            "alert": condition,
            "severity": ALERT_CONDITIONS[condition],
            "alert_fields": fields,
        },
    )
