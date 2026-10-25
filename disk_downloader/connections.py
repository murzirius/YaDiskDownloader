"""Bounded JSON-only connection reuse, isolated by thread and credential epoch.

Media connections keep their independent lifetime and Range safety checks.
A response is reusable only after its entire validated JSON body was consumed.
"""
import threading
import time
from collections import OrderedDict

import requests


class Binding:
    def __init__(self):
        self.current = None

    def __getattr__(self, name):
        if self.current is None:
            raise RuntimeError('No request owns this connection')
        return getattr(self.current, name)


class Lease:
    def __init__(self, pool, key, item):
        self.pool, self.key, self.item = pool, key, item
        self.reusable = False
        self.closed = False

    def get(self, *args, **kwargs):
        return self.item['session'].get(*args, **kwargs)

    def close(self):
        if not self.closed:
            self.closed = True
            self.pool.release(self)


class JsonConnections:
    def __init__(self, adapter_factory):
        self.factory = adapter_factory
        self.lock = threading.Lock()
        self.items = OrderedDict()
        self.epoch = None

    def acquire(self, epoch, deadline):
        key = threading.current_thread()
        with self.lock:
            now = time.monotonic()
            # Expire dead threads/idle sockets and every previous account.
            for thread, item in list(self.items.items()):
                if not item['busy'] and (self.epoch != epoch or not thread.is_alive() or now - item['time'] > 60):
                    item['session'].close()
                    del self.items[thread]
            self.epoch = epoch
            item = self.items.pop(key, None)
            if item and item['busy']:
                self.items[key] = item
                raise RuntimeError('Nested HTTP connection lease')
            if item is None:
                binding = Binding()
                session = requests.Session()
                session.trust_env = False
                session.mount('https://', self.factory(binding))
                session.mount('http://', self.factory(binding))
                item = dict(session=session, binding=binding, epoch=epoch)
            item.update(busy=True, time=now)
            item['binding'].current = deadline
            item['session'].cookies.clear()
            self.items[key] = item
            # Busy sessions are never evicted from another thread.
            for thread, stale in list(self.items.items()):
                if len(self.items) <= 48:
                    break
                if not stale['busy']:
                    stale['session'].close()
                    del self.items[thread]
            return Lease(self, key, item)

    def release(self, lease):
        with self.lock:
            item = lease.item
            item['binding'].current = None
            item['session'].cookies.clear()
            if not lease.reusable or item['epoch'] != self.epoch:
                item['session'].close()
                if self.items.get(lease.key) is item:
                    del self.items[lease.key]
            else:
                item.update(busy=False, time=time.monotonic())

    def close_idle(self):
        with self.lock:
            for thread, item in list(self.items.items()):
                if not item['busy']:
                    item['session'].close()
                    del self.items[thread]
