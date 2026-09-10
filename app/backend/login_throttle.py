"""Bounded in-memory failed-login throttle.

The UAT preview sits on a public URL, so password guessing must cost the caller
something.  Failures are counted per user id AND per client address inside a
sliding window; the tenth failure inside the window refuses further attempts
with 429 ``LOGIN_THROTTLED`` and a ``Retry-After`` until the oldest failure has
aged out.  A successful login clears both keys.

The table is an LRU bounded at ``MAX_KEYS`` so a flood of distinct user ids or
spoofed addresses cannot grow process memory without limit.  It is
process-local by design: with more than one worker the limit is per worker,
which is still a bound, and there is no shared store to poison.

The client address is ``request.client.host``.  The first ``X-Forwarded-For``
hop is honoured only when ``CAPEX_TRUST_PROXY=1``; otherwise an attacker could
rotate the header to defeat the address key.
"""
from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict, deque

from fastapi import HTTPException, Request

WINDOW_SECONDS = 15 * 60
MAX_FAILURES = 10
MAX_KEYS = 10_000


class LoginThrottle:
    def __init__(self, *, window: float = WINDOW_SECONDS, max_failures: int = MAX_FAILURES,
                 max_keys: int = MAX_KEYS, clock=time.monotonic) -> None:
        self.window = float(window)
        self.max_failures = int(max_failures)
        self.max_keys = int(max_keys)
        self._clock = clock
        self._lock = threading.Lock()
        # key -> timestamps of failures still inside the window, oldest first.
        self._failures: OrderedDict[str, deque] = OrderedDict()

    # -- internals ---------------------------------------------------------
    def _prune(self, key: str, now: float):
        stamps = self._failures.get(key)
        if stamps is None:
            return None
        while stamps and now - stamps[0] >= self.window:
            stamps.popleft()
        if not stamps:
            del self._failures[key]
            return None
        self._failures.move_to_end(key)
        return stamps

    # -- public --------------------------------------------------------------
    def retry_after(self, *keys: str) -> int:
        """Seconds until the most-blocked key is unblocked; 0 when none is blocked."""
        now = self._clock()
        wait = 0.0
        with self._lock:
            for key in keys:
                stamps = self._prune(key, now)
                if stamps is not None and len(stamps) >= self.max_failures:
                    wait = max(wait, stamps[0] + self.window - now)
        return int(wait) + 1 if wait > 0 else 0

    def record_failure(self, *keys: str) -> None:
        now = self._clock()
        with self._lock:
            for key in keys:
                stamps = self._prune(key, now)
                if stamps is None:
                    stamps = deque(maxlen=self.max_failures)
                    self._failures[key] = stamps
                stamps.append(now)
            while len(self._failures) > self.max_keys:
                self._failures.popitem(last=False)

    def clear(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._failures)


THROTTLE = LoginThrottle()


def client_address(request: Request) -> str:
    """Peer address, or the first X-Forwarded-For hop when a proxy is trusted."""
    if os.environ.get("CAPEX_TRUST_PROXY", "0") == "1":
        forwarded = request.headers.get("x-forwarded-for", "")
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def keys_for(request: Request, user_id: str) -> tuple[str, str]:
    return (f"user:{user_id}", f"addr:{client_address(request)}")


def refuse_if_throttled(*keys: str) -> None:
    wait = THROTTLE.retry_after(*keys)
    if wait:
        raise HTTPException(
            429, {"code": "LOGIN_THROTTLED",
                  "message": "Too many failed sign-in attempts. Try again later."},
            headers={"Retry-After": str(wait)})
