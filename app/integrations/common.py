"""Small shared helpers for activity integrations."""

import time
import threading


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
        """Return cached value or produce, cache, and return it."""
        cached = self.get(key)
        if cached is not None:
            return cached
        value = producer()
        self.set(key, value, ttl)
        return value


activity_cache = TTLCache()
