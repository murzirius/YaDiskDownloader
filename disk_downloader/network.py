import threading
import time

import requests


class NetworkWait:
    message = "Нет связи с Яндекс.Диском. Проверьте интернет. Работа продолжится автоматически после восстановления связи."

    def __init__(self, event=lambda *args, **kwargs: None, probe=None, interval=1, clock=time.monotonic):
        self.event = event
        self.probe = probe or self.reachable
        self.interval = interval
        self.offline = threading.Event()
        self.lock = threading.Lock()
        self.next_probe = 0
        self.generation = 0
        self.requests_lock = threading.Lock()
        self.requests = set()
        self.clock = clock
        self.waited = 0
        self.wait_started = None
        self.statistics_lock = threading.Lock()

    @property
    def elapsed(self):
        with self.statistics_lock:
            return self.waited + (self.clock() - self.wait_started if self.wait_started is not None else 0)

    def register(self, deadline):
        with self.requests_lock:
            self.requests.add(deadline)
            offline = self.offline.is_set()
        if offline:
            deadline.network_aborted.set()
            deadline.abort()

    def unregister(self, deadline):
        with self.requests_lock:
            self.requests.discard(deadline)

    def abort_requests(self):
        with self.requests_lock:
            active = tuple(self.requests)
        for deadline in active:
            deadline.network_aborted.set()
            deadline.abort()

    def pause_requests(self):
        with self.requests_lock:
            active = tuple(self.requests)
        for deadline in active:
            # A local checksum / fake response has no socket to interrupt and
            # can continue at its checkpoint after the pause without a retry.
            with deadline.lock:
                has_socket = bool(deadline.connections or deadline.sockets)
            if has_socket:
                deadline.pause_aborted.set()
                deadline.abort()

    @staticmethod
    def reachable():
        from .api import APIError, Deadline, DeadlineAdapter
        try:
            with Deadline(3) as deadline, requests.Session() as session:
                session.trust_env = False
                session.mount("https://", DeadlineAdapter(deadline))
                with session.head("https://cloud-api.yandex.net/v1/disk/", timeout=(2, 1), allow_redirects=False) as response:
                    return 100 <= response.status_code <= 599
        except (requests.RequestException, OSError, APIError):
            return False

    def inspect(self, force=False):
        generation = self.generation
        if not self.lock.acquire(blocking=force):
            return
        try:
            if force and generation != self.generation:
                return
            if not force and time.monotonic() < self.next_probe:
                return
            reachable = self.probe()
            self.generation += 1
            self.next_probe = time.monotonic() + self.interval
            if reachable:
                if self.offline.is_set():
                    with self.statistics_lock:
                        if self.wait_started is not None:
                            self.waited += self.clock() - self.wait_started
                            self.wait_started = None
                    self.offline.clear()
                    self.event("system", "Связь с Яндекс.Диском восстановлена. Продолжаем работу.", level="success")
            elif not self.offline.is_set():
                with self.statistics_lock:
                    self.wait_started = self.clock()
                self.offline.set()
                self.abort_requests()
                self.event("system", self.message, level="warning")
        finally:
            self.lock.release()

    def wait(self, checkpoint):
        while self.offline.is_set():
            checkpoint()
            self.inspect()
            if self.offline.is_set():
                time.sleep(0.1)
        checkpoint()

    def recover(self, error, checkpoint):
        from .api import NetworkError
        if not isinstance(error, (NetworkError, requests.RequestException)):
            return False
        checkpoint()
        if self.offline.is_set() or getattr(error, "offline", False):
            self.wait(checkpoint)
            return True
        self.inspect(force=True)
        if not self.offline.is_set():
            return False
        self.wait(checkpoint)
        return True
