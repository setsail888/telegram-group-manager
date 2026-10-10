"""Bounded, per-group sliding windows for joins and welcome messages."""
import time
from collections import OrderedDict, deque


class JoinGuard:
    def __init__(self, max_groups=1000):
        self.max_groups = max_groups
        self.arrivals = OrderedDict()
        self.welcomes = OrderedDict()
        self.last_cleanup = -1e10

    def _recent(self, records, cid, now, window, capacity):
        if now - self.last_cleanup >= 60:
            for mapping in (self.arrivals, self.welcomes):
                for key in list(mapping):
                    if not mapping[key] or now - mapping[key][-1][0] > 300:
                        del mapping[key]
            self.last_cleanup = now
        rows = records.setdefault(cid, deque(maxlen=capacity))
        records.move_to_end(cid)
        while len(records) > self.max_groups:
            records.popitem(last=False)
        while rows and now - rows[0][0] > window:
            rows.popleft()
        return rows

    def arrival(self, cid, uid, *, limit=10, window=60, now=None):
        now = time.monotonic() if now is None else now
        rows = self._recent(self.arrivals, cid, now, window, 100)
        if any(user == uid for _, user in rows):
            return False
        rows.append((now, uid))
        return len(rows) >= limit

    def clear(self, cid):
        self.arrivals.pop(cid, None)

    def welcome(self, cid, *, limit=10, now=None):
        now = time.monotonic() if now is None else now
        rows = self._recent(self.welcomes, cid, now, 60, 60)
        if len(rows) >= limit:
            return False
        rows.append((now, None))
        return True
