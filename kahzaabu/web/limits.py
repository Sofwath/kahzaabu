"""Shared rate-limiter + simple LRU cache for the public Q&A endpoint."""
from __future__ import annotations

import os
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Optional

from slowapi import Limiter
from slowapi.util import get_remote_address

PUBLIC_MODE = bool(os.environ.get("KAHZAABU_PUBLIC_MODE"))
ASK_DAILY_CAP_USD = float(os.environ.get("KAHZAABU_ASK_DAILY_CAP_USD", "5.0"))

# Per-IP limiter
limiter = Limiter(key_func=get_remote_address)


class TTLCache:
    """Very small thread-safe LRU+TTL cache, used for /api/ask responses."""

    def __init__(self, maxsize: int = 100, ttl_seconds: int = 3600):
        self.maxsize = maxsize
        self.ttl = ttl_seconds
        self._d: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()
        self._lock = Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            v = self._d.get(key)
            if not v:
                return None
            ts, value = v
            if time.time() - ts > self.ttl:
                self._d.pop(key, None)
                return None
            self._d.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._d[key] = (time.time(), value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)


ask_cache = TTLCache(maxsize=200, ttl_seconds=3600)
