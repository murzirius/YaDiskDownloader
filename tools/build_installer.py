"""Build the per-user Windows installer (Inno Setup) from the release ZIP.

The installer carries exactly the files of YaDiskDownloader-v<version>-Windows-x64.zip,
so both assets always match. The ZIP name and layout used by the updater are untouched.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'installer' / 'YaDiskDownloader.iss'
PREFIX = 'YaDiskDownloader/'
MAX_UNPACKED = 1024 * 1024 * 1024


def version():
    text = (ROOT / 'disk_downloader/__init__.py').read_text(encoding='utf-8')
    return re.search(r'__version__\s*=\s*"([0-9.]+)"', text).group(1)


def installer_name(app_version=None):
    return f'YaDiskDownloader-v{app_version or version()}-Windows-x64-Setup.exe'


def find_iscc(explicit=None):
    candidates = [explicit, os.environ.get('ISCC'), shutil.which('iscc')]
    for variable in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA'):
        base = os.environ.get(variable)
        if base:
            sub = 'Programs/Inno Setup 6' if variable == 'LOCALAPPDATA' else 'Inno Setup 6'
            candidates.append(Path(base) / sub / 'ISCC.exe')
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise SystemExit('Inno Setup (ISCC.exe) не найден. Установите: winget install JRSoftware.InnoSetup')


def unpack(archive, destination):
    """Extract the ZIP payload only; reject anything outside YaDiskDownloader/."""
    destination = Path(destination).resolve()
    total = 0
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            name = PurePosixPath(info.filename)
            if (info.filename.startswith('/') or chr(92) in info.filename or ':' in info.filename
                    or '..' in name.parts or not info.filename.startswith(PREFIX)):
                raise ValueError('Недопустимое имя в ZIP: ' + info.filename)
            if info.is_dir():
                continue
            total += info.file_size
            if total > MAX_UNPACKED:
                raise ValueError('ZIP слишком большой')
            target = (destination / PurePosixPath(*name.parts[1:])).resolve()
            if not target.is_relative_to(destination):
                raise ValueError('Недопустимое имя в ZIP: ' + info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(info) as stream, target.open('xb') as output:
                shutil.copyfileobj(stream, output, 1024 * 1024)


def build(archive, output, iscc=None):
    from disk_downloader.windows_install import required_files
    app_version = version()
    archive = Path(archive).resolve(strict=True)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    expected = output / installer_name(app_version)
    expected.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='.installer-') as folder:
        stage = Path(folder) / 'stage'
        stage.mkdir()
        unpack(archive, stage)
        missing = [name for name in required_files() if not (stage / name).is_file()]
        if missing:
            raise ValueError('В ZIP нет обязательных файлов: ' + ', '.join(missing))
        command = [str(find_iscc(iscc)), '/Q', f'/DAppVersion={app_version}', f'/DStageDir={stage}',
                   f'/DOutputDir={output}', f'/DOutputBase={expected.stem}', str(SCRIPT)]
        subprocess.run(command, check=True)
    if not expected.is_file() or expected.stat().st_size < 1024 * 1024:
        raise ValueError('Inno Setup не создал установщик')
    return expected


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Сборка установщика Windows')
    parser.add_argument('--zip', type=Path, help='Готовый YaDiskDownloader-v<версия>-Windows-x64.zip')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/release')
    parser.add_argument('--iscc', type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Установщик собирается на Windows')
    sys.path.insert(0, str(ROOT))
    archive = args.zip or args.output / f'YaDiskDownloader-v{version()}-Windows-x64.zip'
    print(build(archive, args.output, args.iscc).name)
