"""Bounded, account-scoped caches and compact status updates.

These caches contain disposable data only. Verified artifacts and durable
progress always remain in SQLite and are never evicted here.
"""
from collections import OrderedDict
import copy
import threading
import time
import secrets


class MemoryCache:
    def __init__(self, max_bytes=32 * 1024 * 1024, max_items=8192, clock=time.monotonic):
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.clock = clock
        self.lock = threading.Lock()
        self.items = OrderedDict()
        self.size = 0
        self.scope = None
        self.hits = self.misses = 0

    def select_scope(self, scope):
        with self.lock:
            if self.scope != scope:
                self.items.clear()
                self.size = 0
                self.scope = scope

    def get(self, key):
        with self.lock:
            item = self.items.get(key)
            if item is not None:
                expires, weight, value = item
                if expires > self.clock():
                    self.items.move_to_end(key)
                    self.hits += 1
                    return value
                self.size -= weight
                del self.items[key]
            self.misses += 1
            return None

    def put(self, key, value, weight, ttl=60):
        if weight < 0 or weight > self.max_bytes:
            return
        with self.lock:
            previous = self.items.pop(key, None)
            if previous:
                self.size -= previous[1]
            self.items[key] = (self.clock() + ttl, weight, value)
            self.size += weight
            while self.size > self.max_bytes or len(self.items) > self.max_items:
                _, (_, size, _) = self.items.popitem(last=False)
                self.size -= size

    def snapshot(self):
        with self.lock:
            return dict(bytes=self.size, limit=self.max_bytes, items=len(self.items), hits=self.hits, misses=self.misses)


class StatusDeltas:
    """One bounded baseline per job; a missed revision causes a full response."""
    def __init__(self):
        self.lock = threading.Lock()
        self.items = OrderedDict()

    def response(self, job, since):
        key = (job['id'], job.get('instance_id'), job.get('auth_revision'))
        with self.lock:
            previous = self.items.pop(key, None)
            revision = secrets.token_hex(8)
            # Exclude only the revision marker itself from comparisons.
            if previous and previous[1] == job:
                revision = previous[0]
            self.items[key] = (revision, copy.deepcopy(job))
            while len(self.items) > 4:
                self.items.popitem(last=False)
            if previous and since == previous[0]:
                old = previous[1]
                return {'delta': True, 'revision': revision,
                        'values': {k: v for k, v in job.items() if k not in old or old[k] != v},
                        'removed': [k for k in old if k not in job]}
            return {'delta': False, 'revision': revision, 'values': job}
