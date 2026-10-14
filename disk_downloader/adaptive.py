import threading
import time


class AdaptiveWorkers:
    def __init__(self, maximum, enabled=True, clock=time.monotonic):
        self.maximum = maximum
        self.enabled = enabled
        self.clock = clock
        self.lock = threading.Lock()
        self.current = maximum
        self.successes = 0
        self.changed = clock()
        self.reason = "Выбранный максимум"
        self.sample_time = clock()
        self.sample_bytes = 0
        self.best_rate = 0
        self.slow_windows = 0

    def configure(self, enabled):
        with self.lock:
            self.enabled = enabled
            if not enabled:
                self.current = self.maximum
                self.reason = "Умное управление выключено"

    def measure(self, transferred, busy):
        if not self.enabled:
            return
        with self.lock:
            current = self.clock()
            elapsed = current - self.sample_time
            if elapsed < 5:
                return
            rate = (transferred - self.sample_bytes) / elapsed
            self.sample_time, self.sample_bytes = current, transferred
            if rate <= 0 or busy < self.current:
                return
            self.best_rate = max(self.best_rate * 0.95, rate)
            self.slow_windows = self.slow_windows + 1 if rate < self.best_rate * 0.5 else 0
            if self.current > 1 and self.slow_windows >= 3 and current - self.changed >= 20:
                self.current -= 1
                self.changed = current
                self.slow_windows = 0
                self.reason = "Снижение после устойчивого падения скорости"

    def limit(self):
        with self.lock:
            return self.current

    def failure(self, status=None):
        if not self.enabled or status is not None and status != 429 and status < 500:
            return
        with self.lock:
            self.successes = 0
            if self.clock() - self.changed < 2 and self.current != self.maximum:
                return
            self.current = max(1, self.current // 2)
            self.changed = self.clock()
            self.reason = "Снижение после сетевых ошибок" if status != 429 else "Снижение после ограничения Яндекса"

    def success(self):
        if not self.enabled:
            return
        with self.lock:
            self.successes += 1
            if self.current < self.maximum and self.successes >= 8 and self.clock() - self.changed >= 5:
                self.current += 1
                self.successes = 0
                self.changed = self.clock()
                self.reason = "Постепенное увеличение при стабильной загрузке"

    def snapshot(self):
        with self.lock:
            return {"current": self.current, "maximum": self.maximum, "enabled": self.enabled, "reason": self.reason}
