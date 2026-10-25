"""Optional per-user installation. Downloads and history are never moved."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from . import __version__
from .paths import safe_join
from .runtime import frozen


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def required_files():
    return ('YaDiskDownloader.exe', 'LICENSE', 'THIRD_PARTY_NOTICES.txt',
            f'source/YaDiskDownloader-v{__version__}-source.zip',
            'third_party/pystray-0.19.5-py2.py3-none-any.whl')


def install_files(source, target):
    source, target = Path(source).resolve(), Path(target).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_join(target.parent, target.name)
    target.mkdir(exist_ok=True)
    marker = Path(safe_join(target, 'install-files.json'))
    owned = {}
    if marker.exists():
        value = json.loads(marker.read_text(encoding='utf-8'))
        if value.get('format') != 1 or value.get('application') != 'YaDiskDownloader' or not isinstance(value.get('files'), dict):
            raise ValueError('На ПК повреждён список установленных файлов. Замена отменена.')
        owned = value['files']
    prepared = {}
    for name in required_files():
        path = Path(safe_join(source, name))
        if not path.is_file():
            raise ValueError('Распакуйте ZIP целиком: рядом с EXE нужны лицензии и папки source и third_party.')
        destination = Path(safe_join(target, name))
        if destination.exists() and (name not in owned or digest(destination) != owned[name]):
            raise ValueError('В папке установки есть изменённый или посторонний файл. Замена отменена.')
        prepared[name] = path
    with tempfile.TemporaryDirectory(prefix='.yadisk-install-', dir=target.parent) as temporary:
        stage = Path(temporary)
        assert stage.resolve().parent == target.parent.resolve()
        records = {}
        for name, path in prepared.items():
            staged = stage / name
            staged.parent.mkdir(parents=True, exist_ok=True)
            with path.open('rb') as src, staged.open('xb') as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
            records[name] = digest(staged)
            if records[name] != digest(path):
                raise ValueError('Приложение изменилось при копировании. Повторите установку.')
        with (stage / 'install-files.json').open('w', encoding='utf-8') as stream:
            json.dump({'format': 1, 'application': 'YaDiskDownloader', 'files': records}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # An open installed EXE fails first, before auxiliary files are touched.
        backups = []
        try:
            for name in (*records, 'install-files.json'):
                destination = Path(safe_join(target, name))
                destination.parent.mkdir(parents=True, exist_ok=True)
                backup = stage / ('backup/' + name)
                if destination.exists():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(destination, backup)
                else:
                    backup = None
                os.replace(stage / name, destination)
                backups.append((destination, backup))
        except BaseException:
            for destination, backup in reversed(backups):
                if backup is not None:
                    os.replace(backup, destination)
                else:
                    destination.unlink()
            raise
    return str(target)


def shortcut(target, menu_dir=None):
    import base64
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = """$ErrorActionPreference='Stop'
$target=Join-Path $env:YADISK_INSTALL_TARGET 'YaDiskDownloader.exe'
$menu=if($env:YADISK_INSTALL_MENU){$env:YADISK_INSTALL_MENU}else{[Environment]::GetFolderPath('Programs')}
$link=Join-Path $menu 'YaDiskDownloader.lnk'
$shell=New-Object -ComObject WScript.Shell
if(Test-Path -LiteralPath $link){$old=$shell.CreateShortcut($link);if($old.TargetPath -ne $target){throw 'A different shortcut already exists'}}
$shortcut=$shell.CreateShortcut($link)
$shortcut.TargetPath=$target
$shortcut.WorkingDirectory=$env:YADISK_INSTALL_TARGET
$shortcut.Save()
"""
    env = os.environ.copy()
    env['YADISK_INSTALL_TARGET'] = str(target)
    env['YADISK_INSTALL_MENU'] = str(menu_dir) if menu_dir else ''
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand',
                             base64.b64encode(script.encode('utf-16le')).decode()],
                            env=env, capture_output=True, timeout=30,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise OSError('Приложение скопировано, но Windows не создала ярлык. Откройте папку установленной программы и запустите EXE оттуда.')


class WindowsInstall:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.result = {'available': os.name == 'nt' and frozen(), 'busy': False, 'message': ''}

    def snapshot(self):
        with self.lock:
            return dict(self.result)

    def start(self):
        if not self.result['available']:
            raise ValueError('Установка доступна в готовом Windows EXE. Для Linux и macOS используйте инструкции своей системы.')
        with self.engine.lock, self.lock:
            self.engine.ensure_idle()
            if self.result['busy']:
                return dict(self.result)
            self.result.update(busy=True, message='Копируем приложение и создаём ярлык…')
            try:
                threading.Thread(target=self.run, daemon=True, name='disk-install').start()
            except BaseException:
                self.result['busy'] = False
                raise
            return dict(self.result)

    def run(self):
        import sys
        try:
            target = Path(os.environ['LOCALAPPDATA']) / 'Programs/YaDiskDownloader'
            install_files(Path(sys.executable).parent, target)
            shortcut(target)
            message = 'Готово. YaDiskDownloader появился в меню «Пуск». История и скачанные файлы сохранены.'
        except Exception as error:
            from .engine import safe_error
            message = 'На ПК не удалось завершить установку. ' + safe_error(error)
        with self.lock:
            self.result.update(busy=False, message=message)
