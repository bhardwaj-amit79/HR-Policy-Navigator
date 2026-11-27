"""Serving utilities: token-bucket rate limiter and TTL response cache (thread-safe)."""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict, defaultdict
from typing import Any, Dict, Optional, Tuple


class RateLimiter:
    """Sliding window: at most `limit` requests per `window_s` per key (session or IP)."""

    def __init__(self, limit: int = 10, window_s: int = 60, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window_s, clock
        self.hits: Dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, key: str) -> Tuple[bool, float]:
        now = self.clock()
        with self._lock:
            q = [t for t in self.hits[key] if now - t < self.window]
            if len(q) >= self.limit:
                self.hits[key] = q
                return False, round(self.window - (now - q[0]), 1)
            q.append(now)
            self.hits[key] = q
            return True, 0.0


class ResponseCache:
    """LRU + TTL cache keyed on the normalised question + strategy. Only successful, final
    answers are cached; refusals and errors are never cached (so a transient outage is not sticky)."""

    def __init__(self, ttl_s: int = 900, max_items: int = 256, clock=time.monotonic):
        self.ttl, self.max, self.clock = ttl_s, max_items, clock
        self.d: "OrderedDict[str, Tuple[float, Dict[str, Any]]]" = OrderedDict()
        self.hits = self.misses = 0
        self._lock = threading.Lock()

    @staticmethod
    def key(query: str, strategy: str, persona: str = "") -> str:
        norm = re.sub(r"\W+", " ", query.lower()).strip()
        return hashlib.sha1(f"{strategy}|{persona}|{norm}".encode()).hexdigest()

    def get(self, query: str, strategy: str, persona: str = "") -> Optional[Dict[str, Any]]:
        k = self.key(query, strategy, persona)
        with self._lock:
            item = self.d.get(k)
            if item and self.clock() - item[0] < self.ttl:
                self.d.move_to_end(k)
                self.hits += 1
                return dict(item[1], cached=True)
            self.d.pop(k, None)
            self.misses += 1
            return None

    def put(self, query: str, strategy: str, result: Dict[str, Any], persona: str = "") -> None:
        if result.get("status") != "success":
            return
        with self._lock:
            self.d[self.key(query, strategy, persona)] = (self.clock(), result)
            while len(self.d) > self.max:
                self.d.popitem(last=False)
