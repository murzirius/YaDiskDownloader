"""Payload accounting; counters never contain URLs, paths or credentials."""
from datetime import date
import math
import threading
import time


class NetworkUsage:
    BLOCK = 1024 * 1024

    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.day = ''
        self.used = self.reserved = 0
        self.limit_mb = 0
        self.requests = self.json_bytes = self.media_bytes = 0
        self.wait_seconds = self.receive_seconds = 0
        with store.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS traffic_usage(day TEXT PRIMARY KEY,bytes INTEGER NOT NULL)')
            row = db.execute("SELECT value FROM preferences WHERE key='traffic_limit_mb'").fetchone()
            if row:
                try:
                    self.limit_mb = self.validate(float(row[0]))
                except (ValueError, TypeError):
                    pass
        with self.lock:
            self._day()

    @staticmethod
    def validate(value):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 1_000_000:
            raise ValueError('Дневной трафик: укажите число МБ от 0 до 1 000 000. 0 — без ограничения.')
        return value

    def _day(self):
        day = date.today().isoformat()
        if self.day != day:
            with self.store.db() as db:
                row = db.execute('SELECT bytes FROM traffic_usage WHERE day=?', (day,)).fetchone()
            self.day = day
            # Conservatively count the last reserved block after an abrupt exit.
            self.used = self.reserved = row[0] if row else 0

    def configure(self, limit):
        limit = self.validate(limit)
        with self.lock:
            with self.store.db() as db:
                db.execute("INSERT OR REPLACE INTO preferences VALUES('traffic_limit_mb',?)", (str(limit),))
            self.limit_mb = limit
        return self.snapshot()

    def exceeded(self):
        with self.lock:
            self._day()
            return bool(self.limit_mb and self.used >= self.limit_mb * self.BLOCK)

    def gate(self, checkpoint):
        if not self.exceeded():
            return
        control = getattr(checkpoint, 'control', None) or getattr(checkpoint, '__self__', None)
        # Deadline.check wraps the actual job checkpoint.
        owner = getattr(control, 'checkpoint', None)
        control = getattr(owner, '__self__', None) or control
        message = 'Достигнут дневной лимит трафика. Увеличьте его в настройках трафика или продолжите завтра. Скачанные файлы сохранены.'
        if not hasattr(control, 'pause'):
            from .api import APIError
            raise APIError(message)
        control.pause_reason = message
        control.pause()
        checkpoint()

    def received(self, count, category):
        with self.lock:
            self._day()
            target = self.used + count
            if target > self.reserved:
                reserved = math.ceil(target / self.BLOCK) * self.BLOCK
                with self.store.db() as db:
                    db.execute('INSERT INTO traffic_usage VALUES(?,?) ON CONFLICT(day) DO UPDATE SET bytes=max(bytes,excluded.bytes)', (self.day, reserved))
                    db.execute("DELETE FROM traffic_usage WHERE day < date(?, '-90 days')", (self.day,))
                self.reserved = reserved
            self.used = target
            if category == 'json':
                self.json_bytes += count
            else:
                self.media_bytes += count

    def request_started(self, seconds):
        with self.lock:
            self.requests += 1
            self.wait_seconds += seconds

    def receiving(self, seconds):
        with self.lock:
            self.receive_seconds += seconds

    def flush(self):
        with self.lock:
            self._day()
            if self.used != self.reserved:
                with self.store.db() as db:
                    db.execute('UPDATE traffic_usage SET bytes=? WHERE day=?', (self.used, self.day))
                self.reserved = self.used

    def snapshot(self):
        with self.lock:
            self._day()
            return dict(day=self.day, bytes=self.used, limit_mb=self.limit_mb,
                        requests=self.requests, json_bytes=self.json_bytes,
                        media_bytes=self.media_bytes, wait_seconds=round(self.wait_seconds, 3),
                        receive_seconds=round(self.receive_seconds, 3))
