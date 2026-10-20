import math
import threading
import time
from contextlib import nullcontext


def validate(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 10000:
        raise ValueError("Скорость: от 0 до 10000 МиБ/с; 0 — без ограничения")
    return float(value)


class RateLimiter:
    def __init__(self, mib, clock=time.monotonic):
        self.lock = threading.Lock()
        self.clock = clock
        self.rate = mib * 1024 * 1024
        self.next = clock()

    def consume(self, size, checkpoint):
        if not self.rate:
            return
        control = getattr(checkpoint,'__self__',None)
        with control.expected_wait() if hasattr(control,'expected_wait') else nullcontext():
            return self._consume(size,checkpoint)

    def _consume(self, size, checkpoint):
        with self.lock:
            self.next = max(self.next, self.clock()) + size / self.rate
            until = self.next
        while True:
            checkpoint()
            delay = until - self.clock()
            if delay <= 0:
                return
            time.sleep(min(0.1, delay))
