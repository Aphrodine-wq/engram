"""
cache.py — Time-based caching for expensive MCP tool operations.

Screen data changes slowly (~10s intervals). Caching tool results
for 30-60s eliminates redundant DB scans and re-computation when
Claude calls the same tool multiple times in a conversation.
"""

import time
import threading
from typing import Any, Optional
from functools import wraps


class Cache:
    """Thread-safe time-based cache."""

    def __init__(self):
        self._store: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str, max_age: float = 60.0) -> Optional[Any]:
        """Get cached value if fresh enough. Returns None on miss."""
        with self._lock:
            if key in self._store:
                ts, value = self._store[key]
                if time.time() - ts < max_age:
                    self._hits += 1
                    return value
                # Expired — remove it
                del self._store[key]
            self._misses += 1
            return None

    def set(self, key: str, value: Any):
        """Store a value with current timestamp."""
        with self._lock:
            self._store[key] = (time.time(), value)

    def invalidate(self, key: str = None):
        """Clear one key or entire cache."""
        with self._lock:
            if key:
                self._store.pop(key, None)
            else:
                self._store.clear()

    def stats(self) -> dict:
        """Cache hit/miss stats."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 2) if total else 0,
                "entries": len(self._store),
            }


# Module singleton
_cache = Cache()


def get_cache() -> Cache:
    return _cache


def cached(key_prefix: str, max_age: float = 60.0):
    """
    Decorator: cache function results for max_age seconds.
    Cache key is built from prefix + args hash.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Build a stable cache key from all args
            arg_key = str(args) + str(sorted(kwargs.items()))
            cache_key = f"{key_prefix}:{hash(arg_key)}"
            result = _cache.get(cache_key, max_age)
            if result is not None:
                return result
            result = func(*args, **kwargs)
            _cache.set(cache_key, result)
            return result
        wrapper.cache_key_prefix = key_prefix
        return wrapper
    return decorator
