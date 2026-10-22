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


def storage_error(error):
    """Explain an OS failure without exposing paths or raw exception strings."""
    code = getattr(error, 'winerror', None) or getattr(error, 'errno', None)
    label = f' Код {"Windows" if getattr(error, "winerror", None) else "ОС"}: {code}.' if code else ''
    if isinstance(error, PermissionError) or getattr(error,'errno',None) == 13 or getattr(error,'winerror',None) == 5:
        reason = 'Накопитель или сетевая папка запретили доступ. Проверьте права и вход в NAS.'
    elif isinstance(error, FileNotFoundError) or code in (2, 3, 15, 21, 53, 67):
        reason = 'Папка загрузки не найдена. Накопитель мог отключиться, измениться буква диска или стать недоступной сетевая папка.'
    elif code in (64, 121, 1231, 1232, 10051, 10054, 10060, 110, 113, 116):
        reason = 'Пропала связь с накопителем или NAS. Проверьте кабель, питание накопителя и подключение к локальной сети.'
    else:
        reason = 'Накопитель не смог прочитать сведения о папке загрузки. Проверьте его подключение и состояние.'
    return 'Папка загрузки недоступна. '+reason+label+' Работа на паузе. Сохранённые файлы не удаляются.'


class UnsafeDestination(OSError):
    pass


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
        # Consecutive completed probes can share a coarse system clock tick.
        self.generation = 0
        self.value = None
        self.identity = None
        self.error = ''
        self.error_code = None
        self.recoverable = True

    def read(self):
        info = os.stat(native(self.root), follow_symlinks=False)
        if os.path.islink(self.root) or getattr(info,'st_file_attributes',0) & 0x400:
            raise UnsafeDestination('Destination is a symlink or reparse point')
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeDestination('Destination is not a directory')
        return (info.st_dev, info.st_ino), shutil.disk_usage(native(self.root))

    def run(self):
        value, error, code, recoverable = None, '', None, True
        try:
            identity, usage = self.probe()
            if self.identity is not None and identity != self.identity:
                error = 'По выбранному пути теперь другая папка или другой накопитель. Автоматическое продолжение заблокировано, чтобы не записать файлы не туда. Верните прежний накопитель и нажмите «Продолжить».'
                recoverable = False
            else:
                self.identity = identity
                value = usage
        except OSError as exc:
            error = storage_error(exc)
            code = getattr(exc,'winerror',None) or exc.errno
            recoverable = self.identity is not None and not isinstance(exc,UnsafeDestination)
            if isinstance(exc,UnsafeDestination):
                error = 'Папка назначения больше не является обычной папкой: вместо неё файл, ссылка или junction. Запись заблокирована. Верните прежнюю папку и нажмите «Продолжить».'
            elif not recoverable:error += ' Прежняя папка ещё не была подтверждена: подключите накопитель и нажмите «Продолжить».'
        except Exception:
            error = 'Приложение на ПК не смогло проверить папку загрузки. Проверьте подключение накопителя и повторите продолжение.'
        finally:
            with self.condition:
                self.slots.release()
                self.value, self.error = value, error
                self.error_code, self.recoverable = code, recoverable
                self.checked = time.monotonic()
                self.generation += 1
                self.running = False
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {'error':self.error, 'error_code':self.error_code,
                    'recoverable':self.recoverable, 'probing':self.running,
                    'identity_confirmed':self.identity is not None, 'available':self.value is not None}

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
                    return None, 'Накопитель или NAS не отвечает на проверку папки за отведённое время. Проверьте питание, кабель и локальную сеть. Работа на паузе; дождитесь ответа накопителя.'
                self.condition.wait(min(.1,left))
            return self.value, self.error


def export_available(control):
    """No new sidecar writes while a destination is known to be unavailable."""
    guard = getattr(control,'destination',None)
    if not guard:return True
    state = guard.snapshot()
    return bool(state['available'] and not state['error'] and getattr(control,'pause_kind','') not in ('storage','storage_manual'))
