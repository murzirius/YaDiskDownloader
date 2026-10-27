"""Throttled startup observations; cancellation remains a separate checkpoint."""
from contextlib import contextmanager
import sqlite3
import time


class HistoryProgress:
    def __init__(self, checkpoint=lambda: None, observer=None):
        self.checkpoint = checkpoint
        self.observer = observer
        self.stage = None
        self.started = self.sent = time.monotonic()
        self.done = self.total = None

    def begin(self, stage):
        self.stage = stage
        self.started = time.monotonic()
        self.done = self.total = None
        self.emit(force=True)

    def emit(self, force=False):
        if self.observer is None or self.stage is None:
            return
        now = time.monotonic()
        if force or now-self.sent >= .25:
            self.sent = now
            # A display failure must never interrupt an already committed copy.
            try:
                self.observer(self.stage, self.done, self.total, max(0, now-self.started))
            except Exception:
                pass

    def check(self):
        self.checkpoint()
        self.emit()

    def copying(self, source):
        page_size = source.execute('PRAGMA page_size').fetchone()[0]
        def progress(status, remaining, total):
            self.done = max(0, total-remaining)*page_size
            self.total = total*page_size
            self.emit(force=status == sqlite3.SQLITE_DONE)
        return progress

    @contextmanager
    def statements(self, db, stage):
        self.begin(stage)
        if self.observer is None:
            yield
            return
        error = None
        def progress():
            nonlocal error
            try:
                self.check()
                return 0
            except BaseException as exc:
                error = exc
                return 1
        db.set_progress_handler(progress, 1000)
        try:
            self.check()
            yield
        except sqlite3.Error:
            if error is not None:
                raise error from None
            raise
        finally:
            db.set_progress_handler(None, 0)
