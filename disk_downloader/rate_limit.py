import math
import threading
import time
from contextlib import nullcontext


def validate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 10000:
        raise ValueError("Скорость: от 0 до 10000 МБ/с; 0 — без ограничения")
    return float(value)


class RateLimiter:
    def __init__(self, mib, clock=time.monotonic):
        self.lock = threading.Lock()
        self.clock = clock
        self.rate = mib * 1024 * 1024
        self.next = clock()
        self.revision = 0

    def configure(self, mib):
        mib = validate(mib)
        with self.lock:
            self.rate = mib*1024*1024
            self.next = self.clock()
            self.revision += 1

    def consume(self, size, checkpoint):
        if not self.rate:
            return
        control = getattr(checkpoint,'__self__',None)
        with control.expected_wait() if hasattr(control,'expected_wait') else nullcontext():
            return self._consume(size,checkpoint)

    def _consume(self, size, checkpoint):
        while True:
            with self.lock:
                if not self.rate:
                    return
                self.next = max(self.next, self.clock()) + size / self.rate
                until, revision = self.next, self.revision
            while True:
                checkpoint()
                with self.lock:
                    changed = revision != self.revision
                if changed:
                    break
                delay = until - self.clock()
                if delay <= 0:
                    return
                time.sleep(min(0.1, delay))
