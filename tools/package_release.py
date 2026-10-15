"""Build distribution ZIPs from an explicit allowlist, without local state."""
import argparse
import hashlib
import os
import re
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
VERSION=re.search(r'__version__\s*=\s*"([0-9.]+)"',(ROOT/'disk_downloader/__init__.py').read_text(encoding='utf-8')).group(1)
ROOT_FILES=('README.md','LICENSE','.gitignore','run.py','build_windows.py','start.cmd','start.ps1','requirements.txt','requirements-dev.txt','requirements-build.txt','requirements-bootstrap.txt')


def source_files():
    for name in ROOT_FILES:
        file=ROOT/name
        if file.is_file(): yield file
    for name in ('disk_downloader','docs','tests','tools','.github'):
        for file in sorted((ROOT/name).rglob('*')):
            if not file.is_file() or file.is_symlink() or any(p in ('__pycache__','.pytest_cache','.venv','.state') for p in file.relative_to(ROOT).parts): continue
            if file.suffix.lower() not in ('.py','.js','.css','.html','.md','.txt','.yml','.yaml'): continue
            yield file


def package(output, executable=None):
    output.mkdir(parents=True,exist_ok=True)
    source=output/f'YaDiskDownloader-v{VERSION}-source.zip'
    with zipfile.ZipFile(source,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for file in source_files(): archive.write(file,'disk-downloader/'+file.relative_to(ROOT).as_posix())
    files=[source]
    if executable:
        executable=executable.resolve()
        if not executable.is_file(): raise ValueError('EXE not found')
        windows=output/f'YaDiskDownloader-v{VERSION}-Windows-x64.zip'
        with zipfile.ZipFile(windows,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            archive.write(executable,'YaDiskDownloader/YaDiskDownloader.exe')
            for name in ('README.md','LICENSE','docs/usage.md','docs/features-v1.3.0.md','docs/architecture.md','docs/testing.md',f'docs/release-v{VERSION}.md'):
                archive.write(ROOT/name,'YaDiskDownloader/'+name)
            archive.write(executable.parent/'THIRD_PARTY_NOTICES.txt','YaDiskDownloader/THIRD_PARTY_NOTICES.txt')
            archive.write(source,'YaDiskDownloader/source/'+source.name)
            wheels=list((executable.parent/'third_party').glob('pystray-0.19.5-*.whl'))
            if len(wheels)!=1: raise ValueError('Original pystray source wheel is required')
            archive.write(wheels[0],'YaDiskDownloader/third_party/'+wheels[0].name)
        files.append(windows)
    for file in files:
        with zipfile.ZipFile(file) as archive:
            if archive.testzip() is not None: raise ValueError('ZIP integrity check failed')
    (output/f'SHA256SUMS-v{VERSION}.txt').write_text(''.join(hashlib.sha256(file.read_bytes()).hexdigest()+'  '+file.name+'\n' for file in files),encoding='ascii')
    return files


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'dist/release');parser.add_argument('--exe',type=Path);parser.add_argument('--check-tag',action='store_true');args=parser.parse_args()
    if args.check_tag:
        if os.environ.get('RELEASE_TAG')!='v'+VERSION: raise SystemExit('Release tag does not match application version')
    else:
        for file in package(args.output,args.exe): print(file.name)
