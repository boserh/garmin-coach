"""In-memory sliding-window rate limiting for the auth endpoints (SEC-01).

Deliberately per-process and in-memory: the bot has no web sessions and the web
layer runs as a single uvicorn process on the Pi, so a shared store (Redis) would
be overkill here. The trade-offs are intentional and must NOT be "fixed" into
global/shared state:

- a process restart resets the counters;
- a second web process would keep its own counters (they wouldn't be shared).

For a single-process Pi deployment that is exactly the right amount of hardening
against brute force / signup spam. Reconsider only if the web ever scales out.
"""
import threading
import time
from collections import deque
from typing import Callable, Optional


class RateLimiter:
    """At most ``limit`` hits per ``window_s`` seconds per key (sliding window).

    ``limit <= 0`` disables the limiter (every call is allowed) — used to turn it
    off in tests and via ``LOGIN_RATE_LIMIT=0``. ``now`` is injectable so tests can
    drive a fake clock without sleeping.
    """

    def __init__(
        self, limit: int, window_s: float, *, now: Callable[[], float] = time.monotonic
    ):
        self.limit = limit
        self.window_s = window_s
        self._now = now
        self._hits: dict[str, deque] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Record an attempt for ``key`` and return whether it is under the limit.

        A blocked attempt (already at the limit) is NOT recorded, so a spammer
        can't keep the window pinned open — it drains as time passes.
        """
        if self.limit <= 0:
            return True
        now = self._now()
        cutoff = now - self.window_s
        with self._lock:
            dq = self._hits.get(key)
            if dq is None:
                dq = deque()
                self._hits[key] = dq
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= self.limit:
                return False
            dq.append(now)
            return True

    def reset(self, key: Optional[str] = None) -> None:
        """Clear one key's history (or all keys). Mainly for tests."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)
