"""Fixed-window counters for login throttling and denial-audit limits (M2, TASK-SEC-008).

A key (lower-cased email, client IP, principal) gets `limit` hits per `window`; the window starts
at the key's first hit. Once a key has reached the limit, `retry_after_s` is the time left in
its window.

ponytail: per-process memory. With several API workers each keeps its own counts, so the
effective limit is `limit x workers`; move to a shared store (PostgreSQL/Redis) for multi-worker
deployments. Keys are pruned when the table grows past `max_keys`.
"""

import math
import threading
from datetime import datetime, timedelta

from app.core.clock import Clock


class WindowCounter:
    def __init__(self, limit: int, window_s: int, clock: Clock, max_keys: int = 50_000) -> None:
        self.limit = limit
        self._window = timedelta(seconds=window_s)
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, tuple[datetime, int]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str, now: datetime) -> tuple[datetime, int] | None:
        entry = self._hits.get(key)
        if entry is not None and now - entry[0] >= self._window:
            del self._hits[key]
            return None
        return entry

    def retry_after_s(self, key: str) -> int:
        """Seconds until `key` may try again; 0 when it is under its limit."""
        now = self._clock.now()
        with self._lock:
            entry = self._live(key, now)
        if entry is None or entry[1] < self.limit:
            return 0
        return max(1, math.ceil((entry[0] + self._window - now).total_seconds()))

    def hit(self, key: str) -> int:
        """Count one event for `key`; returns the count within the current window."""
        now = self._clock.now()
        with self._lock:
            entry = self._live(key, now)
            count = 1 if entry is None else entry[1] + 1
            self._hits[key] = (now if entry is None else entry[0], count)
            if len(self._hits) > self._max_keys:
                self._prune(now)
        return count

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def _prune(self, now: datetime) -> None:
        for key in [k for k, (start, _) in self._hits.items() if now - start >= self._window]:
            del self._hits[key]
        # Still full (a spray of fresh keys): drop the oldest windows first.
        overflow = len(self._hits) - self._max_keys
        if overflow > 0:
            for key, _ in sorted(self._hits.items(), key=lambda kv: kv[1][0])[:overflow]:
                del self._hits[key]
