"""Simple in-memory rate limiting per IP. One server process, so this is all it needs."""
import threading, time
from collections import deque


class RateLimiter:
    def __init__(self):
        self.hits, self.lock = {}, threading.Lock()

    def allow(self, key, limit, per_s, now=None):
        now = time.time() if now is None else now
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and q[0] <= now - per_s: q.popleft()
            if len(q) >= limit: return False
            q.append(now)
            if len(self.hits) > 10000:  # drop idle keys so memory can't grow forever
                self.hits = {k: v for k, v in self.hits.items() if v and v[-1] > now - 3600}
            return True
