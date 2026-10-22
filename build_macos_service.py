"""Build the private macOS app worker without the public Linux CLI."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('dist/service'))
    parser.add_argument('--work', type=Path, default=Path('build/service'))
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Build the application service on macOS')
    root = Path(__file__).resolve().parent
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--console',
               '--noupx', '--name', 'YaDiskDownloader-Service', '--exclude-module', 'disk_downloader.cli',
               '--distpath', str(args.output.resolve()), '--workpath', str(args.work.resolve()),
               '--specpath', str(args.work.resolve()), '--hidden-import', 'run',
               '--add-data', str(root / 'disk_downloader/templates') + ':disk_downloader/templates',
               '--add-data', str(root / 'disk_downloader/static') + ':disk_downloader/static',
               str(root / 'run_macos_service.py')]
    subprocess.run(command, cwd=root, check=True)
    from build_windows import notices
    notices(args.output.resolve())


if __name__ == '__main__':
    main()
