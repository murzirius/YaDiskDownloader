"""Bounded system DNS work; callers can cancel without waiting for the OS."""
from collections import OrderedDict
import socket
import threading
import time


class Resolver:
    def __init__(self, maximum=8, cache_size=128, ttl=30, clock=time.monotonic):
        self.maximum, self.cache_size, self.ttl = maximum, cache_size, ttl
        self.clock = clock
        self.condition = threading.Condition()
        self.pending = {}
        self.cache = OrderedDict()

    def resolve(self, host, port, family, checkpoint):
        key = (host.casefold(), port, family)
        while True:
            checkpoint()
            with self.condition:
                cached = self.cache.get(key)
                if cached:
                    if cached[0] > self.clock():
                        self.cache.move_to_end(key)
                        return cached[1]
                    del self.cache[key]
                job = self.pending.get(key)
                if job is None and len(self.pending) < self.maximum:
                    job = {'done':threading.Event(), 'addresses':(), 'error':None}
                    self.pending[key] = job
                    try:
                        threading.Thread(target=self.lookup, args=(key,job), name='disk-dns', daemon=True).start()
                    except BaseException:
                        del self.pending[key]
                        self.condition.notify_all()
                        raise
                if job is None:
                    # No background queue: occupied slots cannot grow on retries.
                    self.condition.wait(.025)
                    continue
            while not job['done'].wait(.025):
                checkpoint()
            checkpoint()
            if job['error'] is not None:
                raise socket.gaierror(*job['error'])
            return job['addresses']

    def lookup(self, key, job):
        try:
            job['addresses'] = tuple(socket.getaddrinfo(key[0], key[1], key[2], socket.SOCK_STREAM))
        except BaseException as exc:
            # Never share exception tracebacks between request threads.
            job['error'] = (getattr(exc,'errno',None) or socket.EAI_FAIL, 'System DNS lookup failed')
        finally:
            with self.condition:
                if job['error'] is None and job['addresses']:
                    self.cache[key] = (self.clock()+self.ttl, job['addresses'])
                    self.cache.move_to_end(key)
                    while len(self.cache)>self.cache_size:
                        self.cache.popitem(last=False)
                self.pending.pop(key,None)
                job['done'].set()
                self.condition.notify_all()

    def invalidate(self, host, port, family):
        with self.condition:
            self.cache.pop((host.casefold(),port,family),None)
