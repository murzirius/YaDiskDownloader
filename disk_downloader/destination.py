"""Bounded destination probes with a separate wait budget and safe diagnostics.

A blocked OS request remains a single read-only worker. Retries never create
replacement workers while that request is still running.
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


def network_path(root):
    value = os.fspath(root).replace('/', '\\').upper()
    return value.startswith('\\\\?\\UNC\\') or value.startswith('\\\\') and not value.startswith(('\\\\?\\', '\\\\.\\'))


class DestinationGuard:
    slots = threading.BoundedSemaphore(2)

    def __init__(self, root, probe=None, timeout=None, interval=None):
        self.root = root
        self.network = network_path(root)
        self.probe = probe or self.read
        self.timeout = timeout if timeout is not None else 15 if self.network else 2
        self.base_timeout = self.timeout
        self.adaptive_timeout = timeout is None and self.network
        self.base_interval = interval if interval is not None else 5 if self.network else .5
        self.interval = self.base_interval
        self.condition = threading.Condition()
        self.running = False
        self.checked = None
        self.generation = 0
        self.delivered_generation = 0
        self.value = None
        self.identity = None
        self.error = ''
        self.error_code = None
        self.recoverable = True
        self.stage = 'idle'
        self.state = 'idle'
        self.started = None
        self.duration = None
        self.folder_seconds = None
        self.space_seconds = None
        self.timeouts = 0
        self.failures = 0
        self.timed_out = False
        self.error_origin = None

    def read(self):
        started = time.monotonic()
        with self.condition:self.stage = 'folder'
        info = os.stat(native(self.root), follow_symlinks=False)
        if os.path.islink(self.root) or getattr(info,'st_file_attributes',0) & 0x400:
            raise UnsafeDestination('Destination is a symlink or reparse point')
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeDestination('Destination is not a directory')
        with self.condition:
            self.folder_seconds = time.monotonic()-started
            self.stage = 'space'
        started = time.monotonic()
        usage = shutil.disk_usage(native(self.root))
        with self.condition:self.space_seconds = time.monotonic()-started
        return (info.st_dev, info.st_ino), usage

    def run(self):
        value, error, code, recoverable = None, '', None, True
        state, origin = 'available', None
        try:
            identity, usage = self.probe()
            if self.identity is not None and identity != self.identity:
                error = 'По выбранному пути теперь другая папка или другой накопитель. Автоматическое продолжение заблокировано, чтобы не записать файлы не туда. Верните прежний накопитель и нажмите «Продолжить».'
                recoverable, state, origin = False, 'changed', 'validation'
            else:
                self.identity = identity
                value = usage
        except OSError as exc:
            error = storage_error(exc)
            code = getattr(exc,'winerror',None) or exc.errno
            if self.stage == 'space':
                label = f' Код {"Windows" if getattr(exc,"winerror",None) else "ОС"}: {code}.' if code else ''
                error = 'Папка найдена, но не удалось проверить свободное место на накопителе.'+label+' Проверьте доступ к NAS и его состояние. Запись на паузе до успешной проверки места.'
            # Initial connection failures also retry. The first successful
            # probe establishes identity; subsequent probes must match it.
            recoverable = not isinstance(exc, UnsafeDestination)
            state, origin = 'unavailable', 'os'
            if isinstance(exc,UnsafeDestination):
                error = 'Папка назначения больше не является обычной папкой: вместо неё файл, ссылка или junction. Запись заблокирована. Верните прежнюю папку и нажмите «Продолжить».'
                state, origin = 'unsafe', 'validation'
        except Exception:
            error = 'Приложение на ПК не смогло проверить папку загрузки. Повторите продолжение; если ошибка повторяется, сохраните диагностику.'
            recoverable, state, origin = False, 'error', 'application'
        finally:
            with self.condition:
                self.slots.release()
                self.value, self.error = value, error
                self.error_code, self.recoverable = code, recoverable
                self.checked = time.monotonic()
                self.duration = self.checked-self.started
                if self.duration >= self.timeout and not self.timed_out:
                    self.timeouts += 1
                    self.timed_out = True
                self.state, self.error_origin = state, origin
                if error:self.failures += 1
                if self.adaptive_timeout and not error:
                    self.timeout = max(self.base_timeout, min(60, self.duration*2))
                if self.network:
                    self.interval = max(self.base_interval, min(30, self.duration*4))
                self.generation += 1
                self.running = False
                self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            elapsed = max(0,time.monotonic()-self.started) if self.running and self.started is not None else 0
            state = self.state
            if self.running and elapsed >= self.timeout:
                state = 'timeout'
                if not self.timed_out:
                    self.timeouts += 1
                    self.timed_out = True
            elif self.running and elapsed >= min(2,self.timeout):state = 'slow'
            return {'error':self.error, 'error_code':None if self.running else self.error_code,
                    'recoverable':self.recoverable, 'probing':self.running,
                    'identity_confirmed':self.identity is not None,
                    'available':self.value is not None and not self.running,
                    'kind':'network' if self.network else 'local', 'stage':self.stage,
                    'state':state, 'error_origin':'app_timeout' if state=='timeout' else self.error_origin,
                    'elapsed_seconds':round(elapsed,3),
                    'last_duration_seconds':round(self.duration,3) if self.duration is not None else None,
                    'folder_seconds':round(self.folder_seconds,3) if self.folder_seconds is not None else None,
                    'space_seconds':round(self.space_seconds,3) if self.space_seconds is not None else None,
                    'timeout_seconds':self.timeout, 'interval_seconds':self.interval,
                    'completed_checks':self.generation, 'timeouts':self.timeouts, 'failures':self.failures}

    def waiting_message(self):
        stage = 'получение свободного места' if self.stage == 'space' else 'проверка папки'
        if self.started is not None and time.monotonic()-self.started >= self.timeout:
            return f'Накопитель или NAS не отвечает вовремя: {stage}. Приложение ждёт завершения проверки и затем повторит подключение автоматически. Системный вызов ещё не завершился; код ошибки ОС не получен.'
        return f'Проверка накопителя ещё выполняется: {stage}. Ответ задерживается; дождитесь завершения проверки. Скачанные файлы сохранены.'

    def check(self, stopped=lambda: False, force=False, wait_timeout=None):
        with self.condition:
            if stopped():return self.value,self.error
            # A bounded interactive wait may have returned before the worker.
            # Consume its fresh completed result instead of immediately starting
            # another slow probe on every click of Continue.
            if (not self.running and self.generation > self.delivered_generation
                    and self.checked is not None and time.monotonic()-self.checked < self.interval):
                self.delivered_generation = self.generation
                return self.value, self.error
            if not self.running and (force or self.checked is None or time.monotonic()-self.checked >= self.interval):
                if not self.slots.acquire(blocking=False):
                    return None, 'Накопитель ещё не ответил на прежнюю проверку. Приложение продолжит повторные проверки автоматически.'
                self.running = True
                self.started = time.monotonic()
                self.state, self.stage, self.error_origin = 'checking', 'probe', None
                self.folder_seconds = self.space_seconds = None
                self.timed_out = False
                try:
                    threading.Thread(target=self.run, name='disk-destination-probe', daemon=True).start()
                except RuntimeError:
                    self.slots.release()
                    self.running = False
                    self.value = None
                    self.error = 'Приложение на ПК не смогло проверить папку. Закройте лишние программы и повторите продолжение.'
                    self.state, self.error_origin, self.recoverable = 'error', 'application', False
            due = time.monotonic()+(self.timeout if wait_timeout is None else wait_timeout)
            while self.running and not stopped():
                left = due-time.monotonic()
                if left <= 0:
                    if not self.timed_out and time.monotonic()-self.started >= self.timeout:
                        self.timeouts += 1
                        self.timed_out = True
                    return None, self.waiting_message()
                self.condition.wait(min(.1,left))
            if not self.running:
                self.delivered_generation = self.generation
            return self.value, self.error


def export_available(control):
    """No new sidecar writes while a destination is known to be unavailable."""
    guard = getattr(control,'destination',None)
    if not guard:return True
    state = guard.snapshot()
    return bool(state['available'] and not state['error'] and getattr(control,'pause_kind','') not in ('storage','storage_manual'))
