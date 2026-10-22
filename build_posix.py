"""Build the CLI on Linux; never cross-compile an untested binary."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('dist/native'))
    parser.add_argument('--work',type=Path,default=Path('build/native'))
    args=parser.parse_args()
    if sys.platform != 'linux':parser.error('The CLI is available only on Linux')
    root=Path(__file__).resolve().parent
    command=[sys.executable,'-m','PyInstaller','--noconfirm','--onefile','--console','--noupx','--name','yadisk','--distpath',str(args.output.resolve()),'--workpath',str(args.work.resolve()),'--specpath',str(args.work.resolve()),'--add-data',str(root/'disk_downloader/templates')+':disk_downloader/templates','--add-data',str(root/'disk_downloader/static')+':disk_downloader/static','--hidden-import','run',str(root/'run_cli.py')]
    subprocess.run(command,cwd=root,check=True)
    from build_windows import notices
    notices(args.output.resolve())


if __name__=='__main__':main()
