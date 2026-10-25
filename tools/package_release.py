"""Build distribution ZIPs from an explicit allowlist, without local state."""
import argparse
import os
import re
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT=Path(__file__).resolve().parents[1]
VERSION=re.search(r'__version__\s*=\s*"([0-9.]+)"',(ROOT/'disk_downloader/__init__.py').read_text(encoding='utf-8')).group(1)
BUILD=re.search(r'__build__\s*=\s*"([0-9.]+)"',(ROOT/'disk_downloader/__init__.py').read_text(encoding='utf-8')).group(1)
ROOT_FILES=('pyproject.toml','start.sh','build_posix.py','build_macos_service.py','run_cli.py','run_macos_service.py','README.md','LICENSE','.gitignore','run.py','build_windows.py','windows.manifest','requirements.txt','requirements-dev.txt','requirements-build.txt','requirements-bootstrap.txt')
SOURCE_FOLDERS={'disk_downloader','docs','tests','tools','native','.github'}
SOURCE_SUFFIXES={'.py','.js','.cjs','.css','.html','.md','.txt','.yml','.yaml','.svg','.m'}
PRIVATE_NAMES={'agent.md','agents.md','agents.override.md','claude.md','claude.local.md',
               'codex.md','gemini.md','skill.md','copilot-instructions.md',
               'opencode.md','local_settings.py','config.local.py','settings.local.py',
               'token.txt','tokens.txt','credentials.txt','secrets.txt','api_keys.txt'}
PRIVATE_FOLDERS={'__pycache__','node_modules','venv','private','local','outputs',
                 'downloads','dist','build'}
DISTRIBUTION_DOCS=('README.md','LICENSE','docs/usage.md','docs/features-v1.3.0.md',
                   'docs/cli.md','docs/features-v1.5.0.md','docs/validation-v1.5.0.md',
                   'docs/scan-pipeline-validation.md','docs/validation-storage-v1.5.0.md',
                   'docs/macos.md','docs/architecture.md','docs/testing.md',
                   'docs/traffic-and-performance.md', f'docs/release-v{VERSION}.md')


def _tracked_sources():
    """Use this checkout's index, never a parent repository or directory walk."""
    try:
        def git(*args):
            return subprocess.run(['git','-C',str(ROOT),*args],check=True,
                                  capture_output=True).stdout.decode('utf-8')
        top=git('rev-parse','--show-toplevel').strip()
        if Path(top).resolve()!=ROOT.resolve():
            raise ValueError('Папка исходников не совпадает с корнем Git-репозитория. Упаковка отменена.')
        records=git('ls-files','--stage','-z')
        result=[]
        for record in records.split('\0'):
            if not record:
                continue
            header,name=record.split('\t',1)
            mode,_,stage=header.split()
            if stage!='0':
                raise ValueError('В Git остались неразрешённые конфликты. Упаковка отменена.')
            if mode in ('100644','100755'):
                result.append(name)
        return sorted(set(result))
    except (OSError,subprocess.CalledProcessError,UnicodeError) as exc:
        raise ValueError('Не удалось прочитать список исходников Git. Для упаковки нужен полный рабочий репозиторий и установленный Git.') from exc


def _public_source_name(name):
    relative=PurePosixPath(name)
    parts=relative.parts
    if (not parts or relative.is_absolute() or '\\' in name or ':' in name
            or any(part in ('.','..') or any(ord(char)<32 for char in part) for part in parts)):
        return False
    if any(part.casefold() in PRIVATE_NAMES for part in parts):
        return False
    if len(parts)==1:
        return name in ROOT_FILES
    if parts[0] not in SOURCE_FOLDERS or relative.suffix.lower() not in SOURCE_SUFFIXES:
        return False
    return not any(part.startswith('.') or part.casefold() in PRIVATE_FOLDERS for part in parts[1:-1]) and not parts[-1].startswith('.')


def _ordinary_source(file, root):
    try:
        if not file.resolve().is_relative_to(root):
            return False
        current=file
        while True:
            info=os.lstat(current)
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                return False
            if current==file and (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1):
                return False
            if current==ROOT:
                return True
            current=current.parent
    except (OSError,ValueError):
        return False


def source_files():
    root=ROOT.resolve()
    for name in _tracked_sources():
        file=ROOT/name
        if _public_source_name(name) and _ordinary_source(file,root):
            yield file


def package(output, executable=None):
    if executable is None:
        raise ValueError('Windows EXE is required')
    executable=Path(executable).resolve(strict=True)
    if not executable.is_file(): raise ValueError('EXE not found')
    sources=tuple(source_files())
    selected=set(sources)
    if any(ROOT/name not in selected for name in DISTRIBUTION_DOCS):
        raise ValueError('Обязательные инструкции или лицензия не входят в проверенные исходники Git. Упаковка отменена.')
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    windows=output/f'YaDiskDownloader-v{VERSION}-Windows-x64.zip'
    # The source required by dependency licenses stays inside the EXE archive;
    # never leave a separate source or checksum asset in the release directory.
    with tempfile.TemporaryDirectory(prefix='.package-',dir=output) as folder:
        temporary=Path(folder)
        source=temporary/f'YaDiskDownloader-v{VERSION}-source.zip'
        with zipfile.ZipFile(source,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for file in sources: archive.write(file,'disk-downloader/'+file.relative_to(ROOT).as_posix())
        candidate=temporary/windows.name
        with zipfile.ZipFile(candidate,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            archive.write(executable,'YaDiskDownloader/YaDiskDownloader.exe')
            for name in DISTRIBUTION_DOCS:
                archive.write(ROOT/name,'YaDiskDownloader/'+name)
            archive.write(executable.parent/'THIRD_PARTY_NOTICES.txt','YaDiskDownloader/THIRD_PARTY_NOTICES.txt')
            archive.write(source,'YaDiskDownloader/source/'+source.name)
            wheels=list((executable.parent/'third_party').glob('pystray-0.19.5-*.whl'))
            if len(wheels)!=1: raise ValueError('Original pystray source wheel is required')
            archive.write(wheels[0],'YaDiskDownloader/third_party/'+wheels[0].name)
        with zipfile.ZipFile(candidate) as archive:
            if archive.testzip() is not None: raise ValueError('ZIP integrity check failed')
        candidate.replace(windows)
    return [windows]


def valid_release_tag(tag):
    return tag in ('v'+VERSION, 'v'+VERSION+'-build.'+BUILD)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'dist/release');parser.add_argument('--exe',type=Path);parser.add_argument('--check-tag',action='store_true');args=parser.parse_args()
    if args.check_tag:
        if not valid_release_tag(os.environ.get('RELEASE_TAG')): raise SystemExit('Release tag does not match application version and build')
    else:
        if not args.exe: parser.error('Windows EXE is required')
        for file in package(args.output,args.exe): print(file.name)
