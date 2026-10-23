"""Small shared helpers for activity integrations."""

import time
import threading


class UpstreamError(Exception):
    """An upstream source did not answer usefully: no connection, an error
    status, an unreadable reply. Fetchers raise it instead of returning an
    empty list (which means "the source answered with nothing"); the routes
    turn it into a 502, and TTLCache.wrap never stores it."""


class TTLCache:
    """Tiny thread-safe in-process cache for activity responses."""

    def __init__(self):
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str):
        with self._lock:
            hit = self._store.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            if hit:
                self._store.pop(key, None)
            return None

    def set(self, key: str, value, ttl: float):
        with self._lock:
            self._store[key] = (time.monotonic() + ttl, value)

    def wrap(self, key: str, ttl: float, producer):
        """Return cached value or produce, cache, and return it. A producer
        that raises (UpstreamError) caches nothing: the next call retries."""
        cached = self.get(key)
        if cached is not None:
            return cached
        value = producer()
        self.set(key, value, ttl)
        return value


activity_cache = TTLCache()
