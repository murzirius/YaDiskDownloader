"""Startup cancellation and small technical reports; never export user data."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
import threading
import traceback
import uuid

from . import __version__, __build__

PHASES = {
    'imports': 'Загрузка компонентов', 'state': 'Открытие папки приложения',
    'history': 'Резервная копия и подготовка истории',
    'database': 'Открытие базы приложения', 'server': 'Запуск локального сервера',
}
_phase = 'imports'
_cancelled = threading.Event()


class StartupCancelled(Exception):
    pass


def reset():
    global _phase
    _phase = 'imports'
    _cancelled.clear()


def mark(phase):
    global _phase
    if phase not in PHASES:
        raise ValueError('Unknown startup phase')
    _phase = phase
    checkpoint()


def cancel():
    _cancelled.set()


def checkpoint():
    if _cancelled.is_set():
        raise StartupCancelled()


def failure(exc, *, state=None):
    code = uuid.uuid4().hex[:8]
    cause = 'application'
    message = 'Ошибка приложения на ПК. Откройте диагностику и приложите файл к сообщению об ошибке.'
    if type(exc).__name__ == 'BackupTimeout':
        cause = 'history_timeout'
        message = 'Не завершена резервная копия истории. Прежняя история сохранена; откройте диагностику.'
    elif getattr(exc, 'sqlite_errorname', ''):
        cause = 'database'
        message = 'Не удалось открыть историю на ПК. Не удаляйте её; откройте диагностику.'
    elif isinstance(exc, PermissionError):
        cause = 'permission'
        message = 'Windows запретила доступ к файлам приложения. Откройте диагностику.'
    elif isinstance(exc, OSError) and (getattr(exc, 'errno', None) == 28 or getattr(exc, 'winerror', None) == 112):
        cause = 'disk_full'
        message = 'На диске с историей приложения нет места. Освободите место и повторите запуск.'
    elif isinstance(exc, ImportError):
        cause = 'component'
        message = 'Не загрузился компонент приложения. Откройте диагностику.'
    elif isinstance(exc, TimeoutError):
        cause = 'timeout'
        message = 'Операция запуска на ПК не завершилась вовремя. Откройте диагностику.'
    elif re.fullmatch(r'SQLite [0-9.]+ has the WAL-reset bug\. Build with SQLite 3\.51\.3 or newer\.', str(exc)):
        cause = 'sqlite_runtime'
        message = 'Windows загрузила неподходящий SQLite. Откройте диагностику.'
    frames = [{'file': Path(f.filename.replace('\\', '/')).name, 'function': f.name, 'line': f.lineno}
              for f in traceback.extract_tb(exc.__traceback__)[-16:]]
    value = {'id': code, 'time': datetime.now(timezone.utc).isoformat(),
             'version': __version__, 'build': __build__, 'phase': _phase,
             'cause': cause, 'exception': type(exc).__name__, 'frames': frames,
             'sqlite_code': getattr(exc, 'sqlite_errorname', None),
             'os_code': getattr(exc, 'winerror', None) or getattr(exc, 'errno', None),
             'system': platform.system(), 'system_release': platform.release(),
             'architecture': platform.machine(),
             'sqlite_version': getattr(sys.modules.get('sqlite3'), 'sqlite_version', None)}
    if value['sqlite_code']:
        from .sqlite_support import error_details
        value['sqlite'] = error_details(exc)
    if state is None:
        from .runtime import state_folder
        state = state_folder()
    report = None
    for folder in (Path(state) / 'startup-diagnostics', Path(tempfile.gettempdir()) / 'YaDiskDownloader' / 'startup-diagnostics'):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            report = folder / ('startup-' + uuid.uuid4().hex + '.txt')
            with report.open('x', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=False, indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            owned = [p for p in folder.glob('startup-*.txt')
                     if re.fullmatch(r'startup-[a-f0-9]{32}\.txt', p.name) and not p.is_symlink()]
            for old in sorted(owned, key=lambda p: p.stat().st_mtime, reverse=True)[5:]:
                try: old.unlink()
                except OSError: pass
            break
        except OSError:
            report = None
    return {'message': message, 'code': code, 'phase': PHASES.get(_phase, PHASES['imports']), 'report': report}
