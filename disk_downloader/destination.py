"""Bounded destination probes. A blocked NAS never holds an HTTP thread.

Only one probe may run per guard. A timed-out OS call is never replaced by an
unbounded sequence of new threads; the task remains paused until it returns.
"""
import os
import shutil
import stat
import threading
import time
from .paths import native


class StorageRetry(Exception):
    pass


class DestinationGuard:
    slots = threading.BoundedSemaphore(2)
    def __init__(self, root, probe=None, timeout=2, interval=.5):
        self.root = root
        self.probe = probe or self.read
        self.timeout, self.interval = timeout, interval
        self.condition = threading.Condition()
        self.running = False
        self.checked = None
        self.value = None
        self.identity = None
        self.error = ''

    def read(self):
        info = os.stat(native(self.root), follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or os.path.islink(self.root):
            raise OSError('Destination is not a directory')
        return (info.st_dev, info.st_ino), shutil.disk_usage(native(self.root))

    def run(self):
        value, error = None, ''
        try:
            identity, usage = self.probe()
            if self.identity is not None and identity != self.identity:
                error = 'По выбранному пути теперь другая папка. Верните прежний накопитель или создайте дозагрузку с нужной папкой.'
            else:
                self.identity = identity
                value = usage
        except OSError:
            error = 'Папка загрузки недоступна. Проверьте накопитель, сеть и доступ к NAS, затем нажмите «Продолжить».'
        except Exception:
            error = 'Приложение на ПК не смогло проверить папку загрузки. Проверьте подключение накопителя и повторите продолжение.'
        finally:
            with self.condition:
                self.slots.release()
                self.value, self.error = value, error
                self.checked = time.monotonic()
                self.running = False
                self.condition.notify_all()

    def check(self, stopped=lambda: False, force=False):
        with self.condition:
            if stopped():return self.value,self.error
            if not self.running and (force or self.checked is None or time.monotonic()-self.checked >= self.interval):
                if not self.slots.acquire(blocking=False):
                    return None, 'Накопитель ещё не ответил на прежнюю проверку. Подождите восстановления связи и нажмите «Продолжить».'
                self.running = True
                try:
                    threading.Thread(target=self.run, name='disk-destination-probe', daemon=True).start()
                except RuntimeError:
                    self.slots.release()
                    self.running = False
                    self.value = None
                    self.error = 'Приложение на ПК не смогло проверить папку. Закройте лишние программы и повторите продолжение.'
            due = time.monotonic()+self.timeout
            while self.running and not stopped():
                left = due-time.monotonic()
                if left <= 0:
                    return None, 'Папка загрузки не отвечает. Работа на паузе: проверьте накопитель или NAS, затем нажмите «Продолжить».'
                self.condition.wait(min(.1,left))
            return self.value, self.error
