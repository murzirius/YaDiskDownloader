"""Build distribution ZIPs from an explicit allowlist, without local state."""
import argparse
import os
import re
import tempfile
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
VERSION=re.search(r'__version__\s*=\s*"([0-9.]+)"',(ROOT/'disk_downloader/__init__.py').read_text(encoding='utf-8')).group(1)
ROOT_FILES=('pyproject.toml','start.sh','build_posix.py','build_macos_service.py','run_cli.py','run_macos_service.py','README.md','LICENSE','.gitignore','run.py','build_windows.py','windows.manifest','requirements.txt','requirements-dev.txt','requirements-build.txt','requirements-bootstrap.txt')


def source_files():
    for name in ROOT_FILES:
        file=ROOT/name
        if file.is_file(): yield file
    for name in ('disk_downloader','docs','tests','tools','native','.github'):
        for file in sorted((ROOT/name).rglob('*')):
            if not file.is_file() or file.is_symlink() or any(p in ('__pycache__','.pytest_cache','.venv','.state') for p in file.relative_to(ROOT).parts): continue
            if file.suffix.lower() not in ('.py','.js','.cjs','.css','.html','.md','.txt','.yml','.yaml','.svg','.m'): continue
            yield file


def package(output, executable=None):
    if executable is None:
        raise ValueError('Windows EXE is required')
    executable=Path(executable).resolve(strict=True)
    if not executable.is_file(): raise ValueError('EXE not found')
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    windows=output/f'YaDiskDownloader-v{VERSION}-Windows-x64.zip'
    # The source required by dependency licenses stays inside the EXE archive;
    # never leave a separate source or checksum asset in the release directory.
    with tempfile.TemporaryDirectory(prefix='.package-',dir=output) as folder:
        temporary=Path(folder)
        source=temporary/f'YaDiskDownloader-v{VERSION}-source.zip'
        with zipfile.ZipFile(source,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for file in source_files(): archive.write(file,'disk-downloader/'+file.relative_to(ROOT).as_posix())
        candidate=temporary/windows.name
        with zipfile.ZipFile(candidate,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            archive.write(executable,'YaDiskDownloader/YaDiskDownloader.exe')
            for name in ('README.md','LICENSE','docs/usage.md','docs/features-v1.3.0.md','docs/cli.md','docs/features-v1.5.0.md','docs/validation-v1.5.0.md','docs/scan-pipeline-validation.md','docs/macos.md','docs/architecture.md','docs/testing.md',f'docs/release-v{VERSION}.md'):
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


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'dist/release');parser.add_argument('--exe',type=Path);parser.add_argument('--check-tag',action='store_true');args=parser.parse_args()
    if args.check_tag:
        if os.environ.get('RELEASE_TAG')!='v'+VERSION: raise SystemExit('Release tag does not match application version')
    else:
        if not args.exe: parser.error('Windows EXE is required')
        for file in package(args.output,args.exe): print(file.name)
